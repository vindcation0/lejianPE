"""
Preflight 抢课前健康检查系统 (PreflightChecker)
==============================================
实现第四轮优化 Phase 6 要求的独立抢课前综合健康检查系统：
在正式进入抢课轮询前，对全链路进行全维度的诊断与安全检查，
确保在所有 P0 级别安全检查均通过后方可放行。

核心状态与决策：
- READY: 所有 P0 检查全部通过，使用标准服务端检查模式 (SERVER_MODE)
- DEGRADED_READY: checkConflict 故障 (如 OPEN/异常)，但本地快照完整新鲜、本地冲突检查与原子预留完全就绪 (LOCAL_FALLBACK)
- BLOCKED: 任意关键安全检查失败，坚决阻止启动抢课 (BLOCKED)

安全底线：
- 严禁调用真实报名接口 (signUp / submit_once)
- 严禁输出任何 Token、密码、学号等敏感信息
- 严禁污染正式审计日志 (使用独立临时文件测试目录写权限)
- 严禁残留数据库脏写 (SQLite 写入探测通过临时事务强制 ROLLBACK)
- 严禁非法抢占并悬挂单实例锁 (探测锁测试后必须立即释放)
"""
import copy
import dataclasses
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.audit_logger import AuditLogger, EventName, EventType, sanitize_data
from core.circuit_breaker import CircuitBreaker, CircuitState
from core.conflict_checker import (
    ActivityList,
    ConflictCircuitBreaker,
    ConflictManager,
    ConflictState,
    LocalConflictChecker,
)
from core.instance_lock import InstanceLock, InstanceLockError
from core.state_store import RegistrationStatus, StateStore


class PreflightStatus:
    """Preflight 最终判定状态枚举"""
    READY = "READY"
    DEGRADED_READY = "DEGRADED_READY"
    BLOCKED = "BLOCKED"


class PreflightMode:
    """Preflight 启动运行模式枚举 (Phase 7 F01 细化)"""
    SERVER_MODE = "SERVER_MODE"          # activities NORMAL + checkConflict NORMAL (READY)
    LOCAL_CONFLICT = "LOCAL_CONFLICT"    # activities NORMAL + checkConflict OPEN (DEGRADED_READY)
    LOCAL_FALLBACK = "LOCAL_CONFLICT"    # 兼容别名
    ACTIVITY_CACHE = "ACTIVITY_CACHE"    # activities OPEN + 有效缓存 (DEGRADED_READY)
    DEGRADED_FULL = "DEGRADED_FULL"      # 两者均 OPEN + 有效缓存 (DEGRADED_READY)
    BLOCKED = "BLOCKED"                  # 关键安全检查失败 (BLOCKED)


class CheckItemStatus:
    """单项检查状态枚举"""
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class PreflightCheckItem:
    """单项检查详细诊断结果"""
    name: str                           # 检查项标识 (如 auth, state_store 等)
    category: str                       # 检查大类
    status: str                         # OK, WARN, FAIL
    critical: bool                      # 是否为 P0 关键检查项 (FAIL 会导致 BLOCKED)
    message: str                        # 诊断摘要说明
    details: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "critical": self.critical,
            "message": self.message,
            "details": sanitize_data(self.details),
            "duration_ms": round(float(self.duration_ms), 2)
        }


@dataclass
class PreflightReport:
    """Preflight 综合健康检查报告"""
    run_id: str
    start_time: str
    end_time: str
    status: str                         # READY / DEGRADED_READY / BLOCKED
    mode: str                           # SERVER_MODE / LOCAL_FALLBACK / BLOCKED
    user_id: Optional[str] = None
    session_scope_id: Optional[str] = None
    total_duration_ms: float = 0.0
    checks: List[PreflightCheckItem] = field(default_factory=list)
    failure_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "user_id": self.user_id,
            "session_scope_id": self.session_scope_id,
            "status": self.status,
            "mode": self.mode,
            "total_duration_ms": round(float(self.total_duration_ms), 2),
            "failure_reasons": self.failure_reasons,
            "warnings": self.warnings,
            "checks": [c.to_dict() for c in self.checks]
        }

    def to_json(self, path: Optional[str] = None) -> str:
        """导出为脱敏 JSON 字符串，并可选落盘"""
        data = self.to_dict()
        json_str = json.dumps(data, ensure_ascii=False, indent=2)
        if path:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json_str + "\n")
            except Exception:
                pass
        return json_str

    def format_console(self) -> str:
        """格式化为脱敏且高可读性的控制台健康摘要"""
        status_icons = {
            PreflightStatus.READY: "[READY] 通过",
            PreflightStatus.DEGRADED_READY: "[DEGRADED_READY] 降级就绪",
            PreflightStatus.BLOCKED: "[BLOCKED] 安全阻断"
        }
        item_icons = {
            CheckItemStatus.OK: "  [OK]   ",
            CheckItemStatus.WARN: "  [WARN] ",
            CheckItemStatus.FAIL: "  [FAIL] "
        }

        lines = [
            "=" * 64,
            "         lejianPE Preflight 抢课前全链路健康诊断报告",
            "=" * 64,
            f"运行批次 ID (run_id) : {self.run_id}",
            f"检查耗时             : {self.total_duration_ms:.1f} ms",
            f"当前用户范围         : {self.user_id or '未绑定'}",
            f"当前会话范围         : {self.session_scope_id or '未绑定'}",
            f"启动判定结论         : {status_icons.get(self.status, self.status)}",
            f"运行决策模式         : {self.mode}",
            "-" * 64,
            "检查项明细:"
        ]

        for c in self.checks:
            tag = item_icons.get(c.status, "  [??]   ")
            crit = " [P0核心]" if c.critical else ""
            lines.append(f"{tag}{c.name:<18}{crit} : {c.message} ({c.duration_ms:.1f}ms)")

        if self.failure_reasons:
            lines.append("-" * 64)
            lines.append("阻断原因清单:")
            for idx, r in enumerate(self.failure_reasons, 1):
                lines.append(f"  {idx}. {r}")

        if self.warnings:
            lines.append("-" * 64)
            lines.append("风险警告清单:")
            for idx, w in enumerate(self.warnings, 1):
                lines.append(f"  {idx}. {w}")

        lines.append("=" * 64)
        return "\n".join(lines)


class PreflightChecker:
    """
    Preflight 全链路健康检查器。
    在正式抢课前执行 10 大核心健康检查并生成统一诊断报告。
    """

    def __init__(
        self,
        client: Any = None,
        state_store: Optional[StateStore] = None,
        audit_logger: Optional[AuditLogger] = None,
        cfg: Optional[Dict[str, Any]] = None,
        tt: Optional[Dict[str, Any]] = None,
        activities_breaker: Optional[CircuitBreaker] = None,
        conflict_breaker: Optional[ConflictCircuitBreaker] = None,
        local_checker: Optional[LocalConflictChecker] = None,
        instance_lock: Optional[InstanceLock] = None,
        lock_path: Optional[str] = None,
        max_snapshot_age_sec: float = 60.0,
        time_drift_threshold_sec: float = 5.0,
        initial_snapshot: Optional[Any] = None,
        run_id: Optional[str] = None,
        server_time_ref: Optional[float] = None
    ):
        self.client = client
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.cfg = cfg or {}
        self.tt = tt or {}
        self.activities_breaker = activities_breaker
        self.conflict_breaker = conflict_breaker
        self.local_checker = local_checker or LocalConflictChecker()
        self.instance_lock = instance_lock
        self.lock_path = lock_path or os.path.join(ROOT, "data", "grab.lock")
        self.max_snapshot_age_sec = max_snapshot_age_sec
        self.time_drift_threshold_sec = time_drift_threshold_sec
        self.initial_snapshot = initial_snapshot
        self.server_time_ref = server_time_ref

        # 统一 run_id 校验与绑定
        if run_id:
            self.run_id = run_id
        elif self.audit_logger and getattr(self.audit_logger, "run_id", None):
            self.run_id = self.audit_logger.run_id
        else:
            self.run_id = f"preflight_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

        self._token_refresh_lock = threading.RLock()

    # =========================================================================
    # 任务一：认证健康检查 (Task 1: Auth Health Check)
    # =========================================================================
    def check_auth(self) -> PreflightCheckItem:
        """
        检查 Token 存在性、有效期、范围标识及安全刷新机制。
        P03: Token 无效输出 BLOCKED。
        P04: Token 刷新失败输出 BLOCKED。
        P27: Token 刷新并发受到锁保护。
        """
        t0 = time.time()
        details = {
            "token_present": False,
            "token_expired": False,
            "expires_in_sec": None,
            "user_id": None,
            "session_scope_id": None,
            "refreshed": False
        }

        if self.client is None:
            return PreflightCheckItem(
                name="auth",
                category="authentication",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="Client 未配置，无法执行认证健康检查",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 1. 检查凭据基础字段
        st = getattr(self.client, "s", {})
        if not isinstance(st, dict):
            st = {}
        token = st.get("accessToken") or ""
        refresh_token = st.get("refreshToken") or ""

        # 获取范围标识
        user_scope = None
        if hasattr(self.client, "get_user_scope_id") and callable(self.client.get_user_scope_id):
            try:
                user_scope = self.client.get_user_scope_id()
            except Exception:
                pass
        details["user_id"] = user_scope

        sess_scope = None
        if hasattr(self.client, "get_session_scope_id") and callable(self.client.get_session_scope_id):
            try:
                sess_scope = self.client.get_session_scope_id()
            except Exception:
                pass
        details["session_scope_id"] = sess_scope

        if not token or not str(token).strip():
            # 没有 accessToken，尝试看是否有 refreshToken 进行自愈刷新
            if not refresh_token:
                return PreflightCheckItem(
                    name="auth",
                    category="authentication",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message="认证凭据缺失 accessToken 且无 refreshToken，无法通过认证检查",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                )

        details["token_present"] = bool(token and str(token).strip())

        # 2. 检查 Token 过期状态
        exp_sec = -1
        if hasattr(self.client, "expires_in") and callable(self.client.expires_in):
            try:
                exp_sec = self.client.expires_in()
            except Exception:
                exp_sec = -1
        details["expires_in_sec"] = round(exp_sec, 1)

        # 3. 若 Token 已过期或即将过期（<= 0 秒），安全加锁触发刷新 (P04, P27)
        if exp_sec <= 0:
            details["token_expired"] = True
            # 使用 Client 内部的 _refresh_lock 与 PreflightChecker 的互斥锁双重防护
            lock_to_use = getattr(self.client, "_refresh_lock", self._token_refresh_lock)
            with lock_to_use:
                # 再次 double-check 防止多线程并发重复刷新
                if hasattr(self.client, "expires_in") and callable(self.client.expires_in):
                    try:
                        exp_sec = self.client.expires_in()
                    except Exception:
                        exp_sec = -1
                if exp_sec <= 0:
                    try:
                        if hasattr(self.client, "refresh") and callable(self.client.refresh):
                            self.client.refresh()
                            details["refreshed"] = True
                            # 刷新后重新读取范围与有效期
                            if hasattr(self.client, "get_session_scope_id") and callable(self.client.get_session_scope_id):
                                details["session_scope_id"] = self.client.get_session_scope_id()
                            if hasattr(self.client, "expires_in") and callable(self.client.expires_in):
                                details["expires_in_sec"] = round(self.client.expires_in(), 1)
                        else:
                            return PreflightCheckItem(
                                name="auth",
                                category="authentication",
                                status=CheckItemStatus.FAIL,
                                critical=True,
                                message="Token 已过期且 Client 未提供 refresh 方法，认证失败",
                                details=details,
                                duration_ms=(time.time() - t0) * 1000
                            )
                    except Exception as e:
                        # 刷新失败，严格 BLOCKED (P04)
                        return PreflightCheckItem(
                            name="auth",
                            category="authentication",
                            status=CheckItemStatus.FAIL,
                            critical=True,
                            message=f"Token 已过期且自动刷新失败: {e}",
                            details=details,
                            duration_ms=(time.time() - t0) * 1000
                        )

        # 4. 再次验证 Token 有效性 (P03)
        st_after = getattr(self.client, "s", {})
        if not isinstance(st_after, dict):
            st_after = {}
        curr_token = st_after.get("accessToken")
        if not curr_token or not str(curr_token).strip():
            return PreflightCheckItem(
                name="auth",
                category="authentication",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="当前 accessToken 为空或无效",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        return PreflightCheckItem(
            name="auth",
            category="authentication",
            status=CheckItemStatus.OK,
            critical=True,
            message=f"认证凭据有效 (剩余有效期约 {details['expires_in_sec']} 秒, 作用域={details['session_scope_id']})",
            details=details,
            duration_ms=(time.time() - t0) * 1000
        )

    # =========================================================================
    # 任务二：网络与熔断健康检查 (Task 2: Network & Breaker Health Check)
    # =========================================================================
    def check_network(self) -> Tuple[PreflightCheckItem, PreflightCheckItem]:
        """
        分别检查 activities 与 checkConflict 接口熔断器独立健康状态。
        P22: activities 与 checkConflict 健康状态互不影响。
        """
        t0 = time.time()

        # 1. activities 熔断器检查
        act_breaker_state = getattr(self.activities_breaker, "state", CircuitState.CLOSED) if self.activities_breaker else CircuitState.CLOSED
        act_failures = getattr(self.activities_breaker, "failure_count", 0) if self.activities_breaker else 0
        act_details = {
            "circuit_state": str(act_breaker_state),
            "failure_count": act_failures,
            "has_breaker": bool(self.activities_breaker)
        }

        # 检查是否有可用快照缓存 (Phase 7 F01)
        has_valid_cache = False
        snap_to_check = self.initial_snapshot
        if snap_to_check is not None and getattr(snap_to_check, "complete", False) is True:
            age = time.time() - getattr(snap_to_check, "fetched_at", 0)
            if age <= self.max_snapshot_age_sec:
                has_valid_cache = True
        act_details["has_valid_cache"] = has_valid_cache

        if act_breaker_state == CircuitState.OPEN:
            if has_valid_cache:
                act_item = PreflightCheckItem(
                    name="network_activities",
                    category="network",
                    status=CheckItemStatus.WARN,
                    critical=False,
                    message="activities 接口熔断开启 (OPEN)，但存在有效本地快照，降级至缓存模式 (ACTIVITY_CACHE)",
                    details=act_details,
                    duration_ms=(time.time() - t0) * 1000
                )
            else:
                act_item = PreflightCheckItem(
                    name="network_activities",
                    category="network",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message="activities 接口熔断开启 (OPEN) 且无有效快照缓存，禁止启动抢课",
                    details=act_details,
                    duration_ms=(time.time() - t0) * 1000
                )
        else:
            act_item = PreflightCheckItem(
                name="network_activities",
                category="network",
                status=CheckItemStatus.OK,
                critical=True,
                message=f"activities 熔断器处于 {act_breaker_state} 状态 (失败计数: {act_failures})",
                details=act_details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 2. checkConflict 独立熔断器检查
        t1 = time.time()
        conf_breaker_state = getattr(self.conflict_breaker, "state", ConflictState.NORMAL) if self.conflict_breaker else ConflictState.NORMAL
        conf_failures = getattr(self.conflict_breaker, "failure_count", 0) if self.conflict_breaker else 0
        conf_details = {
            "circuit_state": str(conf_breaker_state),
            "failure_count": conf_failures,
            "has_breaker": bool(self.conflict_breaker)
        }

        if conf_breaker_state == ConflictState.OPEN:
            # checkConflict 熔断开启：标记为 WARN，降级至 LOCAL_CONFLICT (P02, F01)
            conf_item = PreflightCheckItem(
                name="network_conflict",
                category="network",
                status=CheckItemStatus.WARN,
                critical=False,
                message="checkConflict 接口熔断开启 (OPEN)，将降级使用本地冲突检查 (LOCAL_CONFLICT)",
                details=conf_details,
                duration_ms=(time.time() - t1) * 1000
            )
        elif conf_breaker_state == ConflictState.HALF_OPEN:
            conf_item = PreflightCheckItem(
                name="network_conflict",
                category="network",
                status=CheckItemStatus.WARN,
                critical=False,
                message="checkConflict 接口处于半开 (HALF_OPEN) 探测中",
                details=conf_details,
                duration_ms=(time.time() - t1) * 1000
            )
        else:
            conf_item = PreflightCheckItem(
                name="network_conflict",
                category="network",
                status=CheckItemStatus.OK,
                critical=False,
                message=f"checkConflict 熔断器处于 {conf_breaker_state} 状态 (失败计数: {conf_failures})",
                details=conf_details,
                duration_ms=(time.time() - t1) * 1000
            )

        return act_item, conf_item

    # =========================================================================
    # 任务三：报名快照检查 (Task 3: ActivityList Snapshot Check)
    # =========================================================================
    def check_snapshot(self, current_snapshot: Optional[Any] = None) -> Tuple[PreflightCheckItem, Optional[Any]]:
        """
        检查本地活动列表快照的完整性、时效性、范围匹配及状态时序。
        P05: 刷新 Token 后旧快照失效。
        P06: activities 接口失败且无有效快照，BLOCKED。
        P07: 报名列表分页不完整，BLOCKED。
        P08: 快照超过有效期且刷新失败，BLOCKED。
        """
        t0 = time.time()
        snap = current_snapshot if current_snapshot is not None else self.initial_snapshot
        details = {
            "has_snapshot": snap is not None,
            "complete": getattr(snap, "complete", None) if snap is not None else None,
            "fetched_at": getattr(snap, "fetched_at", None) if snap is not None else None,
            "age_sec": None,
            "snapshot_user_id": getattr(snap, "user_id", None) if snap is not None else None,
            "snapshot_session_scope_id": getattr(snap, "session_scope_id", None) if snap is not None else None,
            "refreshed": False
        }

        # 1. 若无快照，尝试从 Client 拉取一次新鲜快照
        if snap is None:
            if self.client and hasattr(self.client, "activities") and callable(self.client.activities):
                try:
                    pulled = self.client.activities(page_size=200, req_timeout=6, max_retries=0)
                    if isinstance(pulled, list):
                        snap = pulled
                        details["has_snapshot"] = True
                        details["refreshed"] = True
                        details["complete"] = getattr(snap, "complete", None)
                        details["fetched_at"] = getattr(snap, "fetched_at", None)
                except Exception as pull_err:
                    return PreflightCheckItem(
                        name="snapshot",
                        category="data_integrity",
                        status=CheckItemStatus.FAIL,
                        critical=True,
                        message=f"无现有快照且拉取活动列表失败: {pull_err}",
                        details=details,
                        duration_ms=(time.time() - t0) * 1000
                    ), None
            else:
                return PreflightCheckItem(
                    name="snapshot",
                    category="data_integrity",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message="无可用活动列表快照且 Client 无法获取数据 (P06)",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                ), None

        # 再次确认快照存在性 (P06)
        if snap is None:
            return PreflightCheckItem(
                name="snapshot",
                category="data_integrity",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="未能获取有效活动列表快照",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            ), None

        # 2. 检查分页完整性 complete 标记 (P07)
        is_complete = getattr(snap, "complete", None)
        if is_complete is not True:
            return PreflightCheckItem(
                name="snapshot",
                category="data_integrity",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"活动列表快照不完整 (complete={is_complete})，禁止依据残缺数据进行决策 (P07)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            ), snap

        # 3. 检查快照时效性 (P08)
        now_ts = time.time()
        fetched_at = getattr(snap, "fetched_at", None)
        if fetched_at is None:
            return PreflightCheckItem(
                name="snapshot",
                category="data_integrity",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="快照缺少 fetched_at 时间戳证据",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            ), snap

        age_sec = now_ts - fetched_at
        details["age_sec"] = round(age_sec, 2)

        # 若快照过期，尝试刷新活动列表 (P08)
        if age_sec > self.max_snapshot_age_sec:
            refresh_ok = False
            if self.client and hasattr(self.client, "activities") and callable(self.client.activities):
                try:
                    refreshed = self.client.activities(page_size=200, req_timeout=6, max_retries=0)
                    if isinstance(refreshed, list) and getattr(refreshed, "complete", None) is True:
                        snap = refreshed
                        details["has_snapshot"] = True
                        details["refreshed"] = True
                        details["complete"] = True
                        details["fetched_at"] = getattr(snap, "fetched_at", None)
                        details["age_sec"] = round(time.time() - snap.fetched_at, 2)
                        refresh_ok = True
                except Exception:
                    refresh_ok = False

            if not refresh_ok:
                return PreflightCheckItem(
                    name="snapshot",
                    category="data_integrity",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message=f"快照已过期 ({age_sec:.1f}s > {self.max_snapshot_age_sec}s) 且刷新失败，严禁沿用过期快照 (P08)",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                ), snap

        # 4. 检查用户与会话范围一致性 (P05)
        current_sess = None
        if self.client and hasattr(self.client, "get_session_scope_id") and callable(self.client.get_session_scope_id):
            try:
                current_sess = self.client.get_session_scope_id()
            except Exception:
                pass

        current_u = None
        if self.client and hasattr(self.client, "get_user_scope_id") and callable(self.client.get_user_scope_id):
            try:
                current_u = self.client.get_user_scope_id()
            except Exception:
                pass

        snap_user = getattr(snap, "user_id", None)
        if current_u and snap_user and str(current_u) != str(snap_user):
            return PreflightCheckItem(
                name="snapshot",
                category="data_integrity",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"快照所属用户与当前用户不匹配 (快照: {snap_user}, 当前: {current_u})",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            ), snap

        snap_sess = getattr(snap, "session_scope_id", None)
        if current_sess and snap_sess and str(current_sess) != str(snap_sess):
            # 会话不匹配（如 Token 刚刚刷新过）
            return PreflightCheckItem(
                name="snapshot",
                category="data_integrity",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"快照所属认证会话已失效 (Token 刷新或会话已切换: 快照={snap_sess}, 当前={current_sess}) (P05)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            ), snap

        # 5. 检查快照后是否有本地 StateStore 状态更新
        if self.state_store and hasattr(self.state_store, "get_all_records") and callable(self.state_store.get_all_records):
            try:
                records = self.state_store.get_all_records()
                for r in records:
                    up_at = r.get("updated_at")
                    if up_at and fetched_at:
                        try:
                            # ISO 字符串解析比较
                            up_dt = datetime.datetime.fromisoformat(up_at)
                            up_ts = up_dt.timestamp()
                            if up_ts > fetched_at:
                                return PreflightCheckItem(
                                    name="snapshot",
                                    category="data_integrity",
                                    status=CheckItemStatus.FAIL,
                                    critical=True,
                                    message=f"快照拉取后本地产生了报名状态更新 (id={r.get('activity_id')})，快照已陈旧",
                                    details=details,
                                    duration_ms=(time.time() - t0) * 1000
                                ), snap
                        except Exception:
                            pass
            except Exception:
                pass

        return PreflightCheckItem(
            name="snapshot",
            category="data_integrity",
            status=CheckItemStatus.OK,
            critical=True,
            message=f"快照完整有效 (包含 {len(snap)} 条活动, 时效 {details['age_sec']}s, complete=True)",
            details=details,
            duration_ms=(time.time() - t0) * 1000
        ), snap

    # =========================================================================
    # 任务四：StateStore 健康检查 (Task 4: StateStore Health Check)
    # =========================================================================
    def check_state_store(self) -> PreflightCheckItem:
        """
        检查 SQLite 数据库读写能力与未决记录屏障。
        P09: 存在 UNKNOWN，BLOCKED。
        P10: 存在 SUBMITTING，BLOCKED。
        P11: 存在 VERIFYING，BLOCKED。
        P12: 仅存在 RETRYABLE，重新校验后允许继续。
        P13: StateStore 不可读，BLOCKED。
        P14: StateStore 不可写，BLOCKED。
        P25, P26: 写入测试强制 ROLLBACK，不修改真实报名状态，无脏写。
        """
        t0 = time.time()
        details = {
            "readable": False,
            "writable": False,
            "total_records": 0,
            "unresolved_count": 0,
            "statuses": {}
        }

        if self.state_store is None:
            return PreflightCheckItem(
                name="state_store",
                category="database",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="StateStore 未配置，无法检查持久化状态库",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 1. 读能力检查 (P13)
        try:
            conn = self.state_store._get_conn()
            cur = conn.execute("SELECT COUNT(*) FROM registration_records;")
            count = cur.fetchone()[0]
            details["readable"] = True
            details["total_records"] = count
        except Exception as e:
            return PreflightCheckItem(
                name="state_store",
                category="database",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"StateStore 数据库不可读或表结构损坏: {e} (P13)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 2. 写能力安全探测 (P14, P25, P26: 临时事务 + 强制 ROLLBACK)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            # 创建临时表并探测插入，完成后显式回滚
            conn.execute("CREATE TEMPORARY TABLE IF NOT EXISTS _preflight_probe (id INT);")
            conn.execute("INSERT INTO _preflight_probe VALUES (1);")
            conn.execute("ROLLBACK;")
            details["writable"] = True
        except Exception as e:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            return PreflightCheckItem(
                name="state_store",
                category="database",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"StateStore 数据库不可写或被死锁占用: {e} (P14)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 3. 检查未决状态记录 (P09, P10, P11, P12)
        try:
            all_recs = self.state_store.get_all_records()
            valid_statuses = {
                RegistrationStatus.PENDING,
                RegistrationStatus.SUBMITTING,
                RegistrationStatus.SUCCESS,
                RegistrationStatus.FAILED,
                RegistrationStatus.UNKNOWN,
                RegistrationStatus.VERIFYING,
                RegistrationStatus.RETRYABLE
            }
            unresolved = []
            has_unknown = False
            has_submitting = False
            has_verifying = False
            has_retryable = False

            for r in all_recs:
                st = r.get("status")
                details["statuses"][st] = details["statuses"].get(st, 0) + 1
                if st not in valid_statuses:
                    return PreflightCheckItem(
                        name="state_store",
                        category="database",
                        status=CheckItemStatus.FAIL,
                        critical=True,
                        message=f"检测到损坏或无法识别的报名状态记录: {st} (id={r.get('activity_id')})",
                        details=details,
                        duration_ms=(time.time() - t0) * 1000
                    )
                if st == RegistrationStatus.UNKNOWN:
                    has_unknown = True
                    unresolved.append(r)
                elif st == RegistrationStatus.SUBMITTING:
                    has_submitting = True
                    unresolved.append(r)
                elif st == RegistrationStatus.VERIFYING:
                    has_verifying = True
                    unresolved.append(r)
                elif st == RegistrationStatus.RETRYABLE:
                    has_retryable = True

            details["unresolved_count"] = len(unresolved)

            # 严格判定阻断
            if has_unknown:
                return PreflightCheckItem(
                    name="state_store",
                    category="database",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message=f"检测到存在未决 UNKNOWN 记录，安全屏障生效，必须先启动复核 (P09)",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                )
            if has_submitting:
                return PreflightCheckItem(
                    name="state_store",
                    category="database",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message=f"检测到存在进行中 SUBMITTING 记录，可能存在并发抢课或遗留事务 (P10)",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                )
            if has_verifying:
                return PreflightCheckItem(
                    name="state_store",
                    category="database",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message=f"检测到存在复核中 VERIFYING 记录，禁止直接启动新批次 (P11)",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                )

        except Exception as e:
            return PreflightCheckItem(
                name="state_store",
                category="database",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"查询未决状态记录失败: {e}",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 仅有 RETRYABLE 记录时，允许继续 (P12)
        if has_retryable:
            return PreflightCheckItem(
                name="state_store",
                category="database",
                status=CheckItemStatus.OK,
                critical=True,
                message="StateStore 正常；检测到 RETRYABLE 可重试记录，经校验后允许继续 (P12)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        return PreflightCheckItem(
            name="state_store",
            category="database",
            status=CheckItemStatus.OK,
            critical=True,
            message=f"StateStore 正常 (读写正常, 记录总数 {details['total_records']}, 无未决状态)",
            details=details,
            duration_ms=(time.time() - t0) * 1000
        )

    # =========================================================================
    # 任务五：并发与锁检查 (Task 5: Concurrency & Lock Check)
    # =========================================================================
    def check_concurrency_lock(self) -> PreflightCheckItem:
        """
        检查单实例进程锁及多进程并发冲突。
        P15: InstanceLock 不可用，BLOCKED。
        P16: 存在其他抢课进程时明确报告。
        """
        t0 = time.time()
        details = {
            "lock_path": self.lock_path,
            "already_locked_by_self": False,
            "lock_available": False,
            "conflict_pid": None
        }

        # 1. 若外部已传入并且当前已经由本进程锁定
        if self.instance_lock and getattr(self.instance_lock, "is_locked", False):
            details["already_locked_by_self"] = True
            details["lock_available"] = True
            return PreflightCheckItem(
                name="concurrency_lock",
                category="concurrency",
                status=CheckItemStatus.OK,
                critical=True,
                message=f"单实例锁已由当前运行进程独占持有 (lock={self.lock_path})",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 2. 尝试获取测试锁（绝不悬挂，测试完立即释放）
        test_lock = self.instance_lock or InstanceLock(self.lock_path)
        try:
            acquired = test_lock.acquire()
        except Exception as e:
            # 锁目录不可写或权限错误 (P15)
            return PreflightCheckItem(
                name="concurrency_lock",
                category="concurrency",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"锁文件目录不可用或权限错误: {e} (P15)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        if not acquired:
            # 其他进程正在运行 (P16)
            pid = None
            if os.path.exists(self.lock_path):
                try:
                    with open(self.lock_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        m = re.search(r"pid=(\d+)", content)
                        if m:
                            pid = m.group(1)
                except Exception:
                    pass
            details["conflict_pid"] = pid
            return PreflightCheckItem(
                name="concurrency_lock",
                category="concurrency",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"检测到已有其他抢课进程正在运行 (PID={pid or '未知'}, lock={self.lock_path})，拒绝并发启动 (P16)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 获取成功，立即主动释放测试锁，绝不霸占 (要求 1: 必须立即释放)
        test_lock.release()
        details["lock_available"] = True

        return PreflightCheckItem(
            name="concurrency_lock",
            category="concurrency",
            status=CheckItemStatus.OK,
            critical=True,
            message="单实例运行锁可用且可正常申请与释放",
            details=details,
            duration_ms=(time.time() - t0) * 1000
        )

    # =========================================================================
    # 任务六：配额和业务规则检查 (Task 6: Quota & Business Rule Check)
    # =========================================================================
    def check_quota_rules(self, snapshot: Optional[Any] = None) -> PreflightCheckItem:
        """
        检查周配额、日配额、特殊活动规则及教学周合法性。
        P17: 配额数据不完整，BLOCKED。
        P18: 特殊活动规则无法识别，BLOCKED。
        """
        t0 = time.time()
        details = {
            "max_normal": None,
            "max_daily": None,
            "current_week": None,
            "special_rules_valid": False,
            "registered_normal_count": 0,
            "registered_special_count": 0
        }

        # 1. 检查配额参数 (P17)
        try:
            max_normal = int(self.cfg.get("max_normal", 2))
            if max_normal <= 0:
                raise ValueError("max_normal 必须为正整数")
            details["max_normal"] = max_normal
        except Exception as e:
            return PreflightCheckItem(
                name="quota_rules",
                category="business_rule",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"配置参数 max_normal 缺失或格式非法: {e} (P17)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        try:
            max_daily = int(self.cfg.get("max_daily", 1))
            if max_daily <= 0:
                raise ValueError("max_daily 必须为正整数")
            details["max_daily"] = max_daily
        except Exception as e:
            return PreflightCheckItem(
                name="quota_rules",
                category="business_rule",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"配置参数 max_daily 缺失或格式非法: {e} (P17)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 2. 检查课表与教学周锚点 (P17)
        if not isinstance(self.tt, dict) or "week1_monday" not in self.tt:
            return PreflightCheckItem(
                name="quota_rules",
                category="business_rule",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="课表 timetable 缺少 week1_monday 第一周周一基准日期 (P17)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        try:
            w1_str = str(self.tt["week1_monday"])
            d1 = datetime.date.fromisoformat(w1_str)
            # 计算当前教学周
            today = datetime.date.today()
            cw = (today - d1).days // 7 + 1
            details["current_week"] = cw
        except Exception as e:
            return PreflightCheckItem(
                name="quota_rules",
                category="business_rule",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"课表 week1_monday 格式错误，无法推算当前教学周: {e} (P17)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 3. 检查特殊活动规则解析 (P18)
        spec_cfg = self.cfg.get("special", [])
        if spec_cfg is not None and not isinstance(spec_cfg, (list, tuple, set)):
            return PreflightCheckItem(
                name="quota_rules",
                category="business_rule",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"特殊活动配置 special 必须为列表或集合，当前类型为: {type(spec_cfg)} (P18)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 模拟执行特殊活动规则检查
        try:
            dummy_activity = {"name": "特殊活动测试"}
            # 测试匹配逻辑是否能够无异常执行
            if spec_cfg:
                _ = any(k in dummy_activity["name"] for k in spec_cfg if isinstance(k, str))
            details["special_rules_valid"] = True
        except Exception as e:
            return PreflightCheckItem(
                name="quota_rules",
                category="business_rule",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"特殊活动规则计算异常: {e} (P18)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 4. 统计快照中已报课程数量（若存在快照）
        snap = snapshot if snapshot is not None else self.initial_snapshot
        if snap and isinstance(snap, list):
            norm_cnt = 0
            spec_cnt = 0
            for it in snap:
                if isinstance(it, dict) and it.get("isRegister"):
                    name = it.get("name") or ""
                    is_spec = any(k in name for k in spec_cfg) if spec_cfg else False
                    if is_spec:
                        spec_cnt += 1
                    else:
                        norm_cnt += 1
            details["registered_normal_count"] = norm_cnt
            details["registered_special_count"] = spec_cnt

        return PreflightCheckItem(
            name="quota_rules",
            category="business_rule",
            status=CheckItemStatus.OK,
            critical=True,
            message=f"业务规则与配额合规 (教学周第 {details['current_week']} 周, 周配额 {details['max_normal']}, 日限 {details['max_daily']})",
            details=details,
            duration_ms=(time.time() - t0) * 1000
        )

    # =========================================================================
    # 任务七：审计系统检查 (Task 7: Audit System Check)
    # =========================================================================
    def check_audit_system(self) -> PreflightCheckItem:
        """
        检查审计日志写入能力与降级状态。
        P19: 审计日志不可写，BLOCKED。
        P20: 审计日志测试不污染正式日志。
        """
        t0 = time.time()
        details = {
            "has_logger": self.audit_logger is not None,
            "run_id": getattr(self.audit_logger, "run_id", None) if self.audit_logger else None,
            "writable": False,
            "audit_degraded": False
        }

        if self.audit_logger is None:
            return PreflightCheckItem(
                name="audit_logger",
                category="audit",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="AuditLogger 审计组件未配置，无法保证审计可追溯性",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        logs_dir = getattr(self.audit_logger, "logs_dir", None)
        if not logs_dir:
            return PreflightCheckItem(
                name="audit_logger",
                category="audit",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="AuditLogger 缺少 logs_dir 日志目录配置",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 检查是否已处于降级状态
        is_degraded = getattr(self.audit_logger, "audit_degraded", False)
        details["audit_degraded"] = is_degraded
        if is_degraded:
            return PreflightCheckItem(
                name="audit_logger",
                category="audit",
                status=CheckItemStatus.FAIL,
                critical=True,
                message="审计系统处于降级故障状态 (audit_degraded=True)，关键审计不可用禁止启动 (P19)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 使用独立临时探测文件测试写权限 (P19, P20: 绝对不写入真实 requests/events 文件)
        probe_path = os.path.join(logs_dir, f".preflight_probe_{os.getpid()}_{time.time_ns()}.tmp")
        try:
            os.makedirs(logs_dir, exist_ok=True)
            with open(probe_path, "w", encoding="utf-8") as f:
                f.write(f"probe_{datetime.datetime.now().isoformat()}\n")
                f.flush()
            if os.path.exists(probe_path):
                os.remove(probe_path)
            details["writable"] = True
        except Exception as e:
            return PreflightCheckItem(
                name="audit_logger",
                category="audit",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"审计日志目录不可写或权限拒绝: {e} (P19)",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        return PreflightCheckItem(
            name="audit_logger",
            category="audit",
            status=CheckItemStatus.OK,
            critical=True,
            message=f"审计日志组件正常 (run_id={self.run_id}, 目录写正常)",
            details=details,
            duration_ms=(time.time() - t0) * 1000
        )

    # =========================================================================
    # 任务八：系统时间检查 (Task 8: System Time Check)
    # =========================================================================
    def check_system_time(self, server_time: Optional[float] = None) -> PreflightCheckItem:
        """
        检查本地时间合理性及与服务器时间偏差。
        P21: 系统时间偏差超出阈值，按规则阻断。
        """
        t0 = time.time()
        local_ts = time.time()
        ref_ts = server_time if server_time is not None else self.server_time_ref

        details = {
            "local_timestamp": local_ts,
            "local_time_iso": datetime.datetime.fromtimestamp(local_ts).isoformat(),
            "server_timestamp": ref_ts,
            "drift_sec": None,
            "threshold_sec": self.time_drift_threshold_sec
        }

        # 1. 基础本地时间合理性检查 (大于 2024 年，小于 2050 年)
        if local_ts < 1704067200 or local_ts > 2524608000:
            return PreflightCheckItem(
                name="system_time",
                category="system",
                status=CheckItemStatus.FAIL,
                critical=True,
                message=f"本地系统时钟发生严重错乱 (时间戳={local_ts})，禁止抢课",
                details=details,
                duration_ms=(time.time() - t0) * 1000
            )

        # 2. 服务端时间偏差比对 (P21)
        if ref_ts is not None:
            drift = abs(local_ts - ref_ts)
            details["drift_sec"] = round(drift, 3)
            if drift > self.time_drift_threshold_sec:
                return PreflightCheckItem(
                    name="system_time",
                    category="system",
                    status=CheckItemStatus.FAIL,
                    critical=True,
                    message=f"系统本地时间与服务端时间偏差过大: {drift:.2f}s (允许阈值: {self.time_drift_threshold_sec}s) (P21)",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                )
            else:
                return PreflightCheckItem(
                    name="system_time",
                    category="system",
                    status=CheckItemStatus.OK,
                    critical=True,
                    message=f"系统时间同步正常 (与服务器时间偏差 {drift:.2f}s)",
                    details=details,
                    duration_ms=(time.time() - t0) * 1000
                )

        # 若无明确服务端时间，标记为 WARNING (非 FAIL)，并清晰报告
        return PreflightCheckItem(
            name="system_time",
            category="system",
            status=CheckItemStatus.WARN,
            critical=False,
            message="未能获取服务端时间基准参考，时钟偏差未知 (标记为 WARNING)",
            details=details,
            duration_ms=(time.time() - t0) * 1000
        )

    # =========================================================================
    # 任务九 & 十：全量执行、判定与审计报告 (Tasks 9 & 10)
    # =========================================================================
    def run_all_checks(
        self,
        output_report_path: Optional[str] = None,
        server_time: Optional[float] = None
    ) -> PreflightReport:
        """
        执行所有 Preflight 检查项并生成综合诊断报告与审计日志。
        P01: 正常环境输出 READY。
        P02: checkConflict OPEN 但本地数据有效，输出 DEGRADED_READY。
        P24: 绝不调用 signUp / submit_once。
        P28: 最终报告 run_id 与本次运行完全一致。
        """
        start_ts = time.time()
        start_iso = datetime.datetime.now().isoformat(timespec="milliseconds")

        # 发出 preflight_started 统一审计事件
        if self.audit_logger and hasattr(self.audit_logger, "record_event"):
            try:
                self.audit_logger.record_event(
                    event_type=EventType.PREFLIGHT,
                    event_name=EventName.PREFLIGHT_STARTED,
                    source="preflight",
                    run_id=self.run_id,
                    details={"phase": 6, "mode": "health_check"}
                )
            except Exception:
                pass

        checks: List[PreflightCheckItem] = []
        failure_reasons: List[str] = []
        warnings: List[str] = []

        # 1. 认证检查
        auth_item = self.check_auth()
        checks.append(auth_item)
        if auth_item.status == CheckItemStatus.FAIL and auth_item.critical:
            failure_reasons.append(f"[认证检查失败] {auth_item.message}")
        elif auth_item.status == CheckItemStatus.WARN:
            warnings.append(f"[认证风险] {auth_item.message}")

        # 2. 网络检查 (activities + checkConflict)
        act_item, conf_item = self.check_network()
        checks.append(act_item)
        checks.append(conf_item)
        if act_item.status == CheckItemStatus.FAIL and act_item.critical:
            failure_reasons.append(f"[活动接口故障] {act_item.message}")
        elif act_item.status == CheckItemStatus.WARN:
            warnings.append(f"[活动缓存降级] {act_item.message}")
        if conf_item.status == CheckItemStatus.WARN:
            warnings.append(f"[冲突接口降级] {conf_item.message}")

        # 3. 快照检查
        snap_item, updated_snapshot = self.check_snapshot()
        checks.append(snap_item)
        if snap_item.status == CheckItemStatus.FAIL and snap_item.critical:
            failure_reasons.append(f"[快照完整性阻断] {snap_item.message}")
        elif snap_item.status == CheckItemStatus.WARN:
            warnings.append(f"[快照时效警告] {snap_item.message}")

        # 4. StateStore 检查
        store_item = self.check_state_store()
        checks.append(store_item)
        if store_item.status == CheckItemStatus.FAIL and store_item.critical:
            failure_reasons.append(f"[状态库异常] {store_item.message}")
        elif store_item.status == CheckItemStatus.WARN:
            warnings.append(f"[状态库提示] {store_item.message}")

        # 5. 并发与锁检查
        lock_item = self.check_concurrency_lock()
        checks.append(lock_item)
        if lock_item.status == CheckItemStatus.FAIL and lock_item.critical:
            failure_reasons.append(f"[并发冲突阻断] {lock_item.message}")

        # 6. 配额与规则检查
        quota_item = self.check_quota_rules(snapshot=updated_snapshot)
        checks.append(quota_item)
        if quota_item.status == CheckItemStatus.FAIL and quota_item.critical:
            failure_reasons.append(f"[配额规则异常] {quota_item.message}")
        elif quota_item.status == CheckItemStatus.WARN:
            warnings.append(f"[配额警告] {quota_item.message}")

        # 7. 审计系统检查
        audit_item = self.check_audit_system()
        checks.append(audit_item)
        if audit_item.status == CheckItemStatus.FAIL and audit_item.critical:
            failure_reasons.append(f"[审计系统故障] {audit_item.message}")

        # 8. 系统时间检查
        time_item = self.check_system_time(server_time=server_time)
        checks.append(time_item)
        if time_item.status == CheckItemStatus.FAIL and time_item.critical:
            failure_reasons.append(f"[时间偏差阻断] {time_item.message}")
        elif time_item.status == CheckItemStatus.WARN:
            warnings.append(f"[时间基准警告] {time_item.message}")

        # 9. 最终状态判定 (Task 9 & Phase 7 F01 细化决策树)
        # 只要存在任何关键检查失败 -> BLOCKED
        if failure_reasons:
            final_status = PreflightStatus.BLOCKED
            final_mode = PreflightMode.BLOCKED
        else:
            act_degraded = (act_item.status == CheckItemStatus.WARN)
            conf_degraded = (conf_item.status in (CheckItemStatus.WARN, CheckItemStatus.FAIL))

            if act_degraded and conf_degraded:
                # 两个接口均故障，但本地快照有效且本地冲突规则有效 -> DEGRADED_FULL (DEGRADED_READY)
                final_status = PreflightStatus.DEGRADED_READY
                final_mode = PreflightMode.DEGRADED_FULL
            elif act_degraded:
                # activities 故障但存在有效快照 -> ACTIVITY_CACHE (DEGRADED_READY)
                final_status = PreflightStatus.DEGRADED_READY
                final_mode = PreflightMode.ACTIVITY_CACHE
            elif conf_degraded:
                # checkConflict 故障但本地冲突规则可用 -> LOCAL_CONFLICT (DEGRADED_READY)
                final_status = PreflightStatus.DEGRADED_READY
                final_mode = PreflightMode.LOCAL_CONFLICT
            else:
                # 全链路健康 -> SERVER_MODE (READY)
                final_status = PreflightStatus.READY
                final_mode = PreflightMode.SERVER_MODE

        end_ts = time.time()
        end_iso = datetime.datetime.now().isoformat(timespec="milliseconds")
        dur_ms = (end_ts - start_ts) * 1000

        user_scope = auth_item.details.get("user_id")
        sess_scope = auth_item.details.get("session_scope_id")

        report = PreflightReport(
            run_id=self.run_id,
            start_time=start_iso,
            end_time=end_iso,
            status=final_status,
            mode=final_mode,
            user_id=user_scope,
            session_scope_id=sess_scope,
            total_duration_ms=dur_ms,
            checks=checks,
            failure_reasons=failure_reasons,
            warnings=warnings
        )

        # 导出 JSON 报告
        if output_report_path:
            report.to_json(output_report_path)

        # 10. 发出 preflight 审计结果事件
        if self.audit_logger and hasattr(self.audit_logger, "record_event"):
            try:
                # 记录完成事件
                self.audit_logger.record_event(
                    event_type=EventType.PREFLIGHT,
                    event_name=EventName.PREFLIGHT_CHECK_COMPLETED,
                    status=final_status,
                    source="preflight",
                    run_id=self.run_id,
                    duration_ms=dur_ms,
                    details={
                        "mode": final_mode,
                        "failure_count": len(failure_reasons),
                        "warning_count": len(warnings)
                    }
                )

                # 记录最终状态分流事件
                if final_status == PreflightStatus.READY:
                    self.audit_logger.record_event(
                        event_type=EventType.PREFLIGHT,
                        event_name=EventName.PREFLIGHT_READY,
                        status="READY",
                        source="preflight",
                        run_id=self.run_id
                    )
                elif final_status == PreflightStatus.DEGRADED_READY:
                    self.audit_logger.record_event(
                        event_type=EventType.PREFLIGHT,
                        event_name=EventName.PREFLIGHT_DEGRADED,
                        status="DEGRADED_READY",
                        source="preflight",
                        run_id=self.run_id,
                        details={"mode": "LOCAL_FALLBACK", "warnings": warnings}
                    )
                else:
                    self.audit_logger.record_event(
                        event_type=EventType.PREFLIGHT,
                        event_name=EventName.PREFLIGHT_BLOCKED,
                        status="BLOCKED",
                        source="preflight",
                        run_id=self.run_id,
                        reason="; ".join(failure_reasons),
                        details={"reasons": failure_reasons}
                    )
            except Exception:
                pass

        return report
