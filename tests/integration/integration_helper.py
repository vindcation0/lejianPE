"""
Phase 7 端到端集成测试辅助环境与 Mock 服务端 (integration_helper)
==============================================================
提供全链路端到端集成测试所需的隔离沙箱环境：
1. 独立临时工作目录（包含 SQLite 数据库、单实例锁文件、审计日志目录）
2. 完整模拟乐健服务端的 MockLegymServer（支持 activities, checkConflict, signUp, refreshToken）
3. 故障注入控制器（支持精确注入 HTTP 500/502/504、超时 -1、响应丢失、Token 过期）
4. 全量指标计数器（精确跟踪 HTTP 请求数、signUp 调用数、业务事件数）
"""
import copy
import datetime
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.audit_logger import AuditLogger
from core.circuit_breaker import CircuitBreaker
from core.conflict_checker import (
    ActivityList,
    ConflictCircuitBreaker,
    ConflictManager,
    LocalConflictChecker,
)
from core.instance_lock import InstanceLock
from core.preflight import PreflightChecker, PreflightMode, PreflightStatus
from core.reconciliation import ReconciliationManager
from core.recovery_manager import RecoveryManager
from core.registration_manager import RegistrationManager
from core.request_manager import RequestManager
from core.result_reporter import ResultReporter
from core.retry_policy import RetryPolicy
from core.state_store import RegistrationStatus, StateStore


class MockLegymServer:
    """模拟乐健服务端核心 API 与故障注入机制"""

    def __init__(self):
        self._lock = threading.RLock()
        self.http_requests_count = 0
        self.signup_calls_count = 0
        self.activities_calls_count = 0
        self.conflict_calls_count = 0
        self.refresh_calls_count = 0

        # 服务端存储状态
        self.registered_activities: Set[str] = set()
        self.activities_db: Dict[str, Dict[str, Any]] = {}

        # 故障注入开关
        self.activities_status_code = 200
        self.activities_error: Optional[Exception] = None
        self.activities_complete = True

        self.conflict_status_code = 200
        self.conflict_error: Optional[Exception] = None
        self.conflict_result = False  # False: 无冲突; True: 有冲突

        self.signup_status_code = 200
        self.signup_error: Optional[Exception] = None
        self.signup_business_code = 0  # 0: 成功; 其他: 业务失败
        self.signup_business_msg = "报名成功"
        self.drop_response_after_signup = False  # 模拟服务端执行成功但网络丢包/断开

        self.refresh_status_code = 200
        self.refresh_error: Optional[Exception] = None

    def reset_counts(self):
        with self._lock:
            self.http_requests_count = 0
            self.signup_calls_count = 0
            self.activities_calls_count = 0
            self.conflict_calls_count = 0
            self.refresh_calls_count = 0

    def add_activity(
        self,
        aid: str,
        name: str,
        start_time_ms: int,
        end_time_ms: int,
        start_reg_ms: int = 0,
        end_reg_ms: int = 0,
        limit_people: int = 50,
        registered_people: int = 0,
        is_register: bool = False
    ):
        with self._lock:
            self.activities_db[str(aid)] = {
                "id": str(aid),
                "name": name,
                "startTime": start_time_ms,
                "endTime": end_time_ms,
                "startRegisterTime": start_reg_ms or (int(time.time() * 1000) - 10000),
                "endRegisterTime": end_reg_ms or (int(time.time() * 1000) + 3600000),
                "limitPeopleNumber": limit_people,
                "registerPeople": registered_people,
                "isRegister": is_register
            }
            if is_register:
                self.registered_activities.add(str(aid))

    def handle_activities(self) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            self.http_requests_count += 1
            self.activities_calls_count += 1

            if self.activities_error:
                raise self.activities_error

            if self.activities_status_code != 200:
                return self.activities_status_code, {"code": self.activities_status_code, "msg": "Server Error"}

            items = []
            for aid, it in self.activities_db.items():
                it_copy = dict(it)
                it_copy["isRegister"] = (str(aid) in self.registered_activities)
                items.append(it_copy)

            return 200, {
                "code": 0,
                "msg": "OK",
                "data": {
                    "total": len(items),
                    "items": items
                }
            }

    def handle_check_conflict(self, aid: str) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            self.http_requests_count += 1
            self.conflict_calls_count += 1

            if self.conflict_error:
                raise self.conflict_error

            if self.conflict_status_code != 200:
                return self.conflict_status_code, {"code": self.conflict_status_code, "msg": "Conflict Error"}

            return 200, {
                "code": 0,
                "msg": "OK",
                "data": bool(self.conflict_result)
            }

    def handle_signup(self, aid: str) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            self.http_requests_count += 1
            self.signup_calls_count += 1

            if self.signup_error:
                raise self.signup_error

            if self.drop_response_after_signup:
                # 模拟服务端成功入库但响应丢失 (UNKNOWN 场景)
                self.registered_activities.add(str(aid))
                raise TimeoutError("The write operation timed out waiting for server response")

            if self.signup_status_code != 200:
                return self.signup_status_code, {"code": self.signup_status_code, "msg": "HTTP Error"}

            if self.signup_business_code != 0:
                return 200, {
                    "code": self.signup_business_code,
                    "msg": self.signup_business_msg,
                    "message": self.signup_business_msg
                }

            # 正常报名成功
            self.registered_activities.add(str(aid))
            if str(aid) in self.activities_db:
                self.activities_db[str(aid)]["registerPeople"] += 1
                self.activities_db[str(aid)]["isRegister"] = True

            return 200, {"code": 0, "msg": "报名成功", "data": {"id": str(aid), "success": True}}

    def handle_refresh(self) -> Tuple[int, Dict[str, Any]]:
        with self._lock:
            self.http_requests_count += 1
            self.refresh_calls_count += 1

            if self.refresh_error:
                raise self.refresh_error

            if self.refresh_status_code != 200:
                return self.refresh_status_code, {"code": self.refresh_status_code, "msg": "Refresh Error"}

            new_token = f"token_refreshed_{self.refresh_calls_count}_{time.time()}"
            return 200, {
                "code": 0,
                "msg": "OK",
                "data": {
                    "accessToken": new_token,
                    "refreshToken": "refresh_token_valid",
                    "accessTokenExpires": int((time.time() + 3600) * 1000)
                }
            }


class IntegrationClient:
    """全功能集成客户端，无缝对接 MockLegymServer 与可靠性各层"""

    def __init__(self, server: MockLegymServer, audit_logger: Optional[AuditLogger] = None, token="token_init_123", user_id="user_test_std"):
        self.server = server
        self.audit_logger = audit_logger
        self.token = token
        self.user_id = user_id
        self._expires_in_sec = 3600.0
        self._refresh_lock = threading.RLock()
        self.s = {
            "accessToken": token,
            "refreshToken": "refresh_token_valid",
            "schoolId": "school_integ_01",
            "semesterId": "semester_integ_01",
            "userId": user_id
        }

    def expires_in(self) -> float:
        return self._expires_in_sec

    def get_session_scope_id(self) -> str:
        return "sess_" + hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]

    def get_user_scope_id(self) -> str:
        return "u_" + hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:12]

    def refresh(self) -> str:
        with self._refresh_lock:
            code, resp = self.server.handle_refresh()
            if code != 200 or resp.get("code") != 0:
                raise RuntimeError(f"Token 刷新失败: HTTP {code}")
            d = resp.get("data") or {}
            self.token = d["accessToken"]
            self.s["accessToken"] = self.token
            self._expires_in_sec = 3600.0
            return self.token

    def ensure_token(self, margin=300):
        if self.expires_in() < margin:
            with self._refresh_lock:
                if self.expires_in() < margin:
                    self.refresh()
        return self.s["accessToken"]

    def call(self, method: str, path: str, body: Any = None, timeout: float = 15) -> Tuple[int, Dict[str, Any]]:
        self.ensure_token()
        if "getActivityList" in path:
            return self.server.handle_activities()
        elif "checkConflict" in path:
            aid = (body or {}).get("activityId") or ""
            return self.server.handle_check_conflict(str(aid))
        elif "signUp" in path:
            aid = (body or {}).get("activityId") or ""
            return self.server.handle_signup(str(aid))
        elif "refreshToken" in path:
            return self.server.handle_refresh()
        return 404, {"code": 404, "msg": "Not Found"}

    def activities(self, page_size=200, req_timeout=6, max_retries=0) -> ActivityList:
        code, resp = self.call("POST", "education/app/activity/getActivityList", timeout=req_timeout)
        if code != 200 or resp.get("code") != 0:
            return ActivityList([], complete=False, http_code=code, fetched_at=time.time())
        data = resp.get("data") or {}
        items = data.get("items") or []
        return ActivityList(
            items=items,
            complete=self.server.activities_complete,
            total=len(items),
            fetched=len(items),
            http_code=200,
            fetched_at=time.time(),
            session_scope_id=self.get_session_scope_id(),
            user_id=self.get_user_scope_id()
        )

    def check_conflict(self, activity_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.call("POST", "education/app/activity/checkConflict", {"activityId": str(activity_id)})

    def signUp(self, activity_id: str) -> Tuple[int, Dict[str, Any]]:
        return self.call("POST", "education/app/activity/signUp", {"activityId": str(activity_id)})


class IntegrationEnvironment:
    """端到端集成沙箱环境上下文管理器"""

    def __init__(self, run_id: Optional[str] = None):
        self.test_dir = tempfile.mkdtemp(prefix="integ_env_")
        self.db_path = os.path.join(self.test_dir, "registration.db")
        self.lock_path = os.path.join(self.test_dir, "grab.lock")
        self.logs_dir = os.path.join(self.test_dir, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)

        self.run_id = run_id or f"run_integ_{int(time.time()*1000)}"
        self.server = MockLegymServer()
        self.audit_logger = AuditLogger(self.logs_dir, run_id=self.run_id)
        self.client = IntegrationClient(self.server, audit_logger=self.audit_logger)
        self.state_store = StateStore(self.db_path)
        self.retry_policy = RetryPolicy(base_delay=0.05, max_delay=0.2)
        self.activities_breaker = CircuitBreaker(failure_threshold=3, base_cooldown=0.1, jitter_max=0.0, audit_logger=self.audit_logger)
        self.conflict_breaker = ConflictCircuitBreaker(failure_threshold=3, base_cooldown=0.1, jitter_max=0.0, audit_logger=self.audit_logger)
        self.local_conflict_checker = LocalConflictChecker()
        self.request_manager = RequestManager(self.client, self.retry_policy, audit_logger=self.audit_logger)
        self.registration_manager = RegistrationManager(self.state_store, self.request_manager, audit_logger=self.audit_logger)
        self.recovery_manager = RecoveryManager(self.state_store, self.request_manager, audit_logger=self.audit_logger)
        self.result_reporter = ResultReporter(run_date=datetime.date.today(), run_id=self.run_id)
        self.result_reporter.bind_audit_logger(self.audit_logger)
        self.result_reporter.set_local_state_store(self.state_store)

        self.cfg = {
            "max_normal": 2,
            "max_daily": 1,
            "special": ["游泳专项", "马拉松"],
            "targets": ["篮球", "排球", "羽毛球", "足球"]
        }
        self.tt = {
            "week1_monday": "2026-09-01",
            "total_weeks": 16,
            "courses": []
        }

        self.conflict_manager = ConflictManager(
            client=self.client,
            breaker=self.conflict_breaker,
            local_checker=self.local_conflict_checker,
            audit_logger=self.audit_logger,
            cfg=self.cfg,
            tt=self.tt,
            state_store=self.state_store
        )

        def week_calc_fn(date_obj, tt):
            d = datetime.date.fromisoformat(tt["week1_monday"])
            return (date_obj - d).days // 7 + 1

        self.reconciliation_manager = ReconciliationManager(
            client=self.client,
            cfg=self.cfg,
            timetable=self.tt,
            week_calc_fn=week_calc_fn,
            audit_logger=self.audit_logger,
            state_store=self.state_store
        )

    def create_preflight(self, initial_snapshot: Optional[ActivityList] = None, instance_lock: Optional[InstanceLock] = None) -> PreflightChecker:
        return PreflightChecker(
            client=self.client,
            state_store=self.state_store,
            audit_logger=self.audit_logger,
            cfg=self.cfg,
            tt=self.tt,
            activities_breaker=self.activities_breaker,
            conflict_breaker=self.conflict_breaker,
            local_checker=self.local_conflict_checker,
            instance_lock=instance_lock,
            lock_path=self.lock_path,
            initial_snapshot=initial_snapshot,
            run_id=self.run_id,
            server_time_ref=time.time()
        )

    def submit_signup(self, aid: str, name: str = "", timeout: int = 6):
        """发起单次报名提交"""
        return self.request_manager.submit_once("education/app/activity/signUp", {"activityId": str(aid)}, timeout=timeout)

    def finalize_reporter(self, rec_res: Optional[Dict] = None):
        """同步审计日志、对账结果与 StateStore 到 ResultReporter"""
        from core.audit_logger import AuditLogReader
        try:
            today_str = datetime.datetime.now().strftime("%Y%m%d")
            evt_path = self.audit_logger._get_event_path(today_str)
            if os.path.exists(evt_path):
                run_events = AuditLogReader.read_events_from_file(evt_path)
                audit_summary = AuditLogReader.replay(run_events, run_id=self.audit_logger.run_id)
                self.result_reporter.set_audit_summary(audit_summary)
        except Exception:
            pass

        self.result_reporter.set_local_state_store(self.state_store)
        if rec_res is not None:
            self.result_reporter.set_reconciliation_result(
                reconciled_items=rec_res.get("items", []) if rec_res.get("success") else [],
                unresolved_count=len(self.state_store.get_unresolved_records()),
                normal_registered=rec_res.get("normal_registered", 0),
                max_normal=self.cfg.get("max_normal", 2),
                special_registered=rec_res.get("special_registered", 0),
                max_per_day=int(self.cfg.get("max_per_day", 1)),
                weekly_exceeded=bool(rec_res.get("exceeded_weeks")),
                daily_exceeded=bool(rec_res.get("exceeded_days")),
                reconciliation_report=rec_res
            )

    def close(self):
        try:
            shutil.rmtree(self.test_dir)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
