"""
统一审计日志与事件模型模块 (AuditLogger & AuditLogReader)
======================================================
实现第四轮优化 Phase 3 要求的统一、准确、可追溯事件审计体系：
1. 统一事件 Schema (Schema Version 1)。
2. 严格区分 5 类事件：
   - candidate: 候选评估 (discovered, check_started, accepted, rejected)
   - submission: 真实报名提交 (started, response_received, succeeded, failed, unknown)
   - verification: UNKNOWN 服务端复核 (started, request_succeeded, request_failed, confirmed_registered, confirmed_unregistered, remains_unknown)
   - reconciliation: 全局对账 (started, succeeded, failed, incomplete)
   - infrastructure: 底层 HTTP 与服务故障 (http_request_started, http_request_succeeded, http_request_failed, circuit_open, circuit_half_open, circuit_closed, infrastructure_recovered)
3. 严格脱敏：禁止记录 AccessToken, RefreshToken, 密码, 手机号, 学号等敏感信息。
4. 提供 AuditLogReader 与幂等事件回放机制。
"""
import collections
import datetime
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class EventType:
    """审计事件分类"""
    CANDIDATE = "candidate"
    SUBMISSION = "submission"
    VERIFICATION = "verification"
    RECONCILIATION = "reconciliation"
    INFRASTRUCTURE = "infrastructure"
    CONFLICT = "conflict"


class EventName:
    """审计事件明确枚举"""
    # 1. Candidate 候选评估
    CANDIDATE_DISCOVERED = "candidate_discovered"
    CANDIDATE_CHECK_STARTED = "candidate_check_started"
    CANDIDATE_ACCEPTED = "candidate_accepted"
    CANDIDATE_REJECTED = "candidate_rejected"

    # 2. Submission 真实报名提交 (仅在实际发起 submit_once 时产生)
    SUBMISSION_STARTED = "submission_started"
    SUBMISSION_RESPONSE_RECEIVED = "submission_response_received"
    SUBMISSION_SUCCEEDED = "submission_succeeded"
    SUBMISSION_FAILED = "submission_failed"
    SUBMISSION_UNKNOWN = "submission_unknown"

    # 3. Verification UNKNOWN 服务端复核 (与真实 Submission 完全分离)
    VERIFICATION_STARTED = "verification_started"
    VERIFICATION_REQUEST_SUCCEEDED = "verification_request_succeeded"
    VERIFICATION_REQUEST_FAILED = "verification_request_failed"
    VERIFICATION_CONFIRMED_REGISTERED = "verification_confirmed_registered"
    VERIFICATION_CONFIRMED_UNREGISTERED = "verification_confirmed_unregistered"
    VERIFICATION_REMAINS_UNKNOWN = "verification_remains_unknown"

    # 4. Reconciliation 全局对账
    RECONCILIATION_STARTED = "reconciliation_started"
    RECONCILIATION_SUCCEEDED = "reconciliation_succeeded"
    RECONCILIATION_FAILED = "reconciliation_failed"
    RECONCILIATION_INCOMPLETE = "reconciliation_incomplete"

    # 5. Infrastructure 底层 HTTP 与故障熔断
    HTTP_REQUEST_STARTED = "http_request_started"
    HTTP_REQUEST_SUCCEEDED = "http_request_succeeded"
    HTTP_REQUEST_FAILED = "http_request_failed"
    CIRCUIT_OPEN = "circuit_open"
    CIRCUIT_HALF_OPEN = "circuit_half_open"
    CIRCUIT_CLOSED = "circuit_closed"
    INFRASTRUCTURE_RECOVERED = "infrastructure_recovered"

    # 6. Conflict Check 冲突检查与智能降级 (Phase 5)
    CONFLICT_CHECK_STARTED = "conflict_check_started"
    CONFLICT_CHECK_SUCCEEDED = "conflict_check_succeeded"
    CONFLICT_CHECK_FAILED = "conflict_check_failed"
    CONFLICT_CHECK_DEGRADED = "conflict_check_degraded"
    LOCAL_CONFLICT_CHECK_STARTED = "local_conflict_check_started"
    LOCAL_CONFLICT_CHECK_SUCCEEDED = "local_conflict_check_succeeded"
    LOCAL_CONFLICT_DETECTED = "local_conflict_detected"
    LOCAL_CONFLICT_CHECK_BLOCKED = "local_conflict_check_blocked"
    CONFLICT_SERVICE_RECOVERED = "conflict_service_recovered"


def sanitize_data(val: Any) -> Any:
    """递归脱敏敏感字段（Token、密码、手机号、学号、凭据等）"""
    if isinstance(val, dict):
        sanitized = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("token", "secret", "password", "auth", "cookie", "sign", "signature", "credential")):
                sanitized[k] = "***MASKED***"
            elif any(s in k_lower for s in ("mobile", "phone", "student", "usercode", "usernumber", "realname", "identity")):
                sanitized[k] = "***MASKED***"
            else:
                sanitized[k] = sanitize_data(v)
        return sanitized
    elif isinstance(val, (list, tuple, set)):
        return [sanitize_data(item) for item in val]
    elif isinstance(val, str):
        # 针对字符串中可能的 Bearer Token 做脱敏
        val = re.sub(r"(Bearer\s+)[a-zA-Z0-9_\-\.]+", r"\1***MASKED***", val, flags=re.IGNORECASE)
        # 针对 11 位手机号脱敏
        val = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "1**********", val)
        # 针对长 Token 串脱敏 (例如 JWT 或 32位十六进制散列)
        val = re.sub(r"(?i)(token[\"':\s=]+)[a-zA-Z0-9_\-\.]{16,}", r"\1***MASKED***", val)
        return val
    return val


class AuditLogger:
    """统一审计日志记录器"""

    def __init__(self, logs_dir: str, run_id: Optional[str] = None):
        self.logs_dir = os.path.abspath(logs_dir)
        os.makedirs(self.logs_dir, exist_ok=True)
        self._seq = 0
        self.write_error_count = 0
        # 同一次程序运行使用全局统一的 run_id
        now_dt = datetime.datetime.now()
        self.run_id = run_id or f"run_{now_dt.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"

    def _get_request_path(self, date_str: str) -> str:
        return os.path.join(self.logs_dir, f"requests-{date_str}.jsonl")

    def _get_event_path(self, date_str: str) -> str:
        return os.path.join(self.logs_dir, f"events-{date_str}.jsonl")

    def _mark_run_degraded(self, run_id: Optional[str]):
        """在日志目录中持久化标记降级状态，防进程崩溃后丢失降级信息"""
        if not run_id:
            return
        try:
            marker_path = os.path.join(self.logs_dir, f".degraded_{run_id}")
            with open(marker_path, "w", encoding="utf-8") as f:
                f.write(f"degraded at {datetime.datetime.now().isoformat()}\n")
        except Exception:
            pass

    def record_event(
        self,
        event_type: str,
        event_name: str,
        activity_id: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        http_status: Optional[int] = None,
        duration_ms: Optional[float] = None,
        source: str = "core",
        correlation_id: Optional[str] = None,
        request_id: Optional[str] = None,
        details: Optional[Dict] = None,
        timestamp: Optional[datetime.datetime] = None,
        event_id: Optional[str] = None,
        run_id: Optional[str] = None
    ) -> Dict:
        """
        核心方法：记录一个符合统一 Schema (Version 1) 的审计事件。
        自动脱敏、分配唯一 event_id、关联 run_id 与 correlation_id、request_id。
        """
        self._seq += 1
        ts = timestamp or datetime.datetime.now()
        ts_iso = ts.isoformat(timespec="milliseconds")
        today_str = ts.strftime("%Y%m%d")

        evt_id = event_id or f"evt_{ts.strftime('%Y%m%d%H%M%S')}_{self._seq:04d}_{uuid.uuid4().hex[:6]}"
        corr_id = correlation_id or evt_id

        record = {
            "schema_version": 1,
            "event_id": evt_id,
            "run_id": run_id or self.run_id,
            "correlation_id": corr_id,
            "request_id": str(request_id) if request_id is not None else None,
            "timestamp": ts_iso,
            "event_type": str(event_type),
            "event_name": str(event_name),
            "activity_id": str(activity_id) if activity_id is not None else None,
            "status": str(status) if status is not None else None,
            "reason": sanitize_data(reason) if reason else None,
            "http_status": int(http_status) if http_status is not None else None,
            "duration_ms": round(float(duration_ms), 2) if duration_ms is not None else None,
            "source": str(source),
            "details": sanitize_data(details) if details else {},
            # 向后兼容字段 (保证旧读取器兼容)
            "event": str(event_name),
            "t": ts_iso
        }

        line = json.dumps(record, ensure_ascii=False)
        try:
            with open(self._get_event_path(today_str), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            self.write_error_count += 1
            self._mark_run_degraded(self.run_id)
            # 写入失败必须有明确处理和警告日志，绝不抛出未经捕获的异常打断主业务流程
            print(f"[警告] 写入审计事件日志失败 (event_id={evt_id}): {e}", file=sys.stderr)

        return record

    def log_event(self, event_type: str, details: Optional[Dict] = None):
        """向后兼容接口：映射至统一事件模型"""
        now = datetime.datetime.now()
        det = details or {}

        # 映射旧版事件类型
        name_lower = event_type.lower()
        if "fail" in name_lower or "500" in name_lower or "502" in name_lower or "503" in name_lower:
            cat = EventType.INFRASTRUCTURE
            evt_name = EventName.HTTP_REQUEST_FAILED
        elif "ok" in name_lower or "recover" in name_lower:
            cat = EventType.INFRASTRUCTURE
            evt_name = EventName.INFRASTRUCTURE_RECOVERED
        elif "verify" in name_lower:
            cat = EventType.VERIFICATION
            evt_name = event_type
        else:
            cat = EventType.INFRASTRUCTURE
            evt_name = event_type

        return self.record_event(
            event_type=cat,
            event_name=evt_name,
            activity_id=str(det.get("aid") or det.get("activity_id") or "") or None,
            reason=det.get("reason"),
            http_status=det.get("http") or det.get("http_status") or det.get("http_code"),
            source="legacy_log_event",
            details=det,
            timestamp=now
        )

    def log_request(
        self,
        activity_id: str,
        activity_name: str,
        action: str,
        started_at: datetime.datetime,
        finished_at: datetime.datetime,
        http_status: int,
        business_code: Optional[int] = None,
        business_success: Optional[bool] = None,
        registration_verified: Optional[bool] = None,
        final_state: str = "UNKNOWN",
        attempts: int = 1,
        error_message: Optional[str] = None,
        request_id: Optional[str] = None
    ) -> Dict:
        """记录单次提交/复核请求审计条目 (写 requests-YYYYMMDD.jsonl，同时保留格式兼容)"""
        self._seq += 1
        dur_ms = round((finished_at - started_at).total_seconds() * 1000, 2)
        today_str = started_at.strftime("%Y%m%d")

        if not request_id:
            request_id = f"{started_at.strftime('%Y%m%d%H%M%S')}-{self._seq:04d}-{uuid.uuid4().hex[:6]}"

        record = {
            "request_id": request_id,
            "activity_id": str(activity_id),
            "activity_name": str(activity_name),
            "action": str(action),
            "started_at": started_at.isoformat(timespec="milliseconds"),
            "finished_at": finished_at.isoformat(timespec="milliseconds"),
            "duration_ms": dur_ms,
            "http_status": http_status,
            "business_code": business_code,
            "business_success": business_success,
            "registration_verified": registration_verified,
            "final_state": final_state,
            "attempts": attempts,
            "error_message": sanitize_data(error_message) if error_message else None
        }

        line = json.dumps(record, ensure_ascii=False)
        try:
            with open(self._get_request_path(today_str), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            self.write_error_count += 1
            self._mark_run_degraded(self.run_id)
            print(f"[警告] 写入请求审计日志失败: {e}", file=sys.stderr)

        return record


class AuditLogReader:
    """审计日志解析与事件统计回放器"""

    @staticmethod
    def parse_line(line: str) -> Optional[Dict]:
        """解析单行 JSONL 审计事件，兼容 Schema v1 与旧版格式"""
        raw = line.strip()
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None

            # 若为 Schema v1 标准格式
            if "schema_version" in data and "event_type" in data:
                if "request_id" not in data:
                    data["request_id"] = None
                return data

            # 若为旧版事件格式: {"t": "...", "event": "...", ...}
            legacy_evt = data.get("event") or "legacy_event"
            legacy_t = data.get("t") or datetime.datetime.now().isoformat()
            legacy_http = data.get("http") or data.get("http_status")
            return {
                "schema_version": 0,
                "event_id": data.get("event_id") or f"legacy_{hash(raw)}",
                "run_id": data.get("run_id") or "legacy_run",
                "correlation_id": data.get("correlation_id") or data.get("event_id") or "legacy_corr",
                "request_id": data.get("request_id"),
                "timestamp": legacy_t,
                "event_type": EventType.INFRASTRUCTURE if ("fail" in legacy_evt or "list" in legacy_evt) else EventType.SUBMISSION,
                "event_name": legacy_evt,
                "activity_id": str(data.get("activity_id") or data.get("aid") or "") or None,
                "status": data.get("status"),
                "reason": data.get("reason"),
                "http_status": legacy_http,
                "duration_ms": data.get("duration_ms"),
                "source": "legacy_reader",
                "details": data,
                "event": legacy_evt,
                "t": legacy_t
            }
        except Exception:
            return None

    @classmethod
    def read_events_from_file(cls, file_path: str) -> List[Dict]:
        """从 JSONL 文件中读取所有有效事件"""
        if not os.path.exists(file_path):
            return []
        events = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                parsed = cls.parse_line(line)
                if parsed:
                    events.append(parsed)
        return events

    @classmethod
    def replay(cls, events: List[Dict], run_id: Optional[str] = None, log_dir: Optional[str] = None) -> Dict[str, Any]:
        """
        基于事件日志的幂等统计回放：
        1. 基于 event_id 去重，重复处理绝不产生二次计数（A13）；
        2. 严格区分 Candidate、Submission、Verification、Reconciliation、Infrastructure；
        3. 聚合 HTTP 请求与失败状态码分布。
        """
        seen_event_ids: Set[str] = set()
        deduped_events: List[Dict] = []

        for evt in events:
            eid = evt.get("event_id")
            if eid:
                if eid in seen_event_ids:
                    continue
                seen_event_ids.add(eid)
            # 若过滤特定 run_id
            if run_id and evt.get("run_id") != run_id:
                continue
            deduped_events.append(evt)

        # 统计计数器
        candidate_count = 0
        candidate_rejected = 0
        candidate_accepted = 0
        candidate_blocked = 0
        candidate_reasons = collections.Counter()

        # Submission (仅当实际调用 submit_once 时产生)
        submission_started_count = 0
        submission_succeeded_count = 0
        submission_failed_count = 0
        submission_unknown_count = 0

        # Verification (与 Submission 严格分离)
        verification_started_count = 0
        verification_confirmed_registered_count = 0
        verification_confirmed_unregistered_count = 0
        verification_remains_unknown_count = 0
        verification_durations: List[float] = []

        # Reconciliation
        reconciliation_started_count = 0
        reconciliation_succeeded_count = 0
        reconciliation_failed_count = 0

        # Infrastructure HTTP 请求
        http_requests_total = 0
        http_requests_succeeded = 0
        http_requests_failed = 0
        http_failures_by_code = collections.Counter()
        http_durations: List[float] = []

        # 请求级关联与配对跟踪 (request_id)
        started_request_ids: Set[str] = set()
        finished_request_ids: Set[str] = set()

        # 熔断器状态切换
        circuit_trips = 0
        circuit_half_open_count = 0
        circuit_closed_count = 0
        outage_seconds_accumulated = 0.0

        # 冲突检查与智能降级统计 (Phase 5)
        conflict_check_started_count = 0
        conflict_check_succeeded_count = 0
        conflict_check_failed_count = 0
        conflict_check_degraded_count = 0
        local_conflict_check_started_count = 0
        local_conflict_check_succeeded_count = 0
        local_conflict_detected_count = 0
        local_conflict_check_blocked_count = 0
        conflict_service_recovered_count = 0

        runs_observed: Set[str] = set()
        correlations_observed: Set[str] = set()

        for evt in deduped_events:
            etype = evt.get("event_type")
            ename = evt.get("event_name")
            rid = evt.get("run_id")
            cid = evt.get("correlation_id")
            req_id = evt.get("request_id")
            code = evt.get("http_status")
            dur = evt.get("duration_ms")
            reas = evt.get("reason")

            if rid:
                runs_observed.add(rid)
            if cid:
                correlations_observed.add(cid)

            # 请求级配对跟踪
            if req_id:
                if ename == EventName.HTTP_REQUEST_STARTED:
                    started_request_ids.add(req_id)
                elif ename in (EventName.HTTP_REQUEST_SUCCEEDED, EventName.HTTP_REQUEST_FAILED):
                    finished_request_ids.add(req_id)

            # 1. Candidate 候选评估
            if etype == EventType.CANDIDATE:
                candidate_count += 1
                if ename == EventName.CANDIDATE_REJECTED:
                    candidate_rejected += 1
                    r_str = str(reas or "规则拦截")
                    candidate_reasons[r_str] += 1
                    if any(k in r_str for k in ("屏障", "上限", "满额", "已满", "冻结")):
                        candidate_blocked += 1
                elif ename == EventName.CANDIDATE_ACCEPTED:
                    candidate_accepted += 1

            # 2. Submission 真实报名提交
            elif etype == EventType.SUBMISSION:
                if ename == EventName.SUBMISSION_STARTED:
                    submission_started_count += 1
                elif ename == EventName.SUBMISSION_SUCCEEDED:
                    submission_succeeded_count += 1
                elif ename == EventName.SUBMISSION_FAILED:
                    submission_failed_count += 1
                elif ename == EventName.SUBMISSION_UNKNOWN:
                    submission_unknown_count += 1

            # 3. Verification 服务端复核
            elif etype == EventType.VERIFICATION:
                if dur is not None:
                    try:
                        verification_durations.append(float(dur))
                    except Exception:
                        pass
                if ename == EventName.VERIFICATION_STARTED:
                    verification_started_count += 1
                elif ename == EventName.VERIFICATION_CONFIRMED_REGISTERED:
                    verification_confirmed_registered_count += 1
                elif ename == EventName.VERIFICATION_CONFIRMED_UNREGISTERED:
                    verification_confirmed_unregistered_count += 1
                elif ename == EventName.VERIFICATION_REMAINS_UNKNOWN:
                    verification_remains_unknown_count += 1

            # 4. Reconciliation 全局对账
            elif etype == EventType.RECONCILIATION:
                if ename == EventName.RECONCILIATION_STARTED:
                    reconciliation_started_count += 1
                elif ename == EventName.RECONCILIATION_SUCCEEDED:
                    reconciliation_succeeded_count += 1
                elif ename == EventName.RECONCILIATION_FAILED:
                    reconciliation_failed_count += 1

            # 5. Infrastructure
            elif etype == EventType.INFRASTRUCTURE:
                if dur is not None and ename in (EventName.HTTP_REQUEST_SUCCEEDED, EventName.HTTP_REQUEST_FAILED):
                    try:
                        http_durations.append(float(dur))
                    except Exception:
                        pass

                if ename == EventName.HTTP_REQUEST_STARTED:
                    http_requests_total += 1
                elif ename == EventName.HTTP_REQUEST_SUCCEEDED:
                    http_requests_succeeded += 1
                elif ename in (EventName.HTTP_REQUEST_FAILED, "list_fail"):
                    http_requests_failed += 1
                    if code is not None:
                        http_failures_by_code[int(code)] += 1
                elif ename == EventName.CIRCUIT_OPEN:
                    circuit_trips += 1
                elif ename == EventName.CIRCUIT_HALF_OPEN:
                    circuit_half_open_count += 1
                elif ename in (EventName.CIRCUIT_CLOSED, EventName.INFRASTRUCTURE_RECOVERED, "list_ok"):
                    circuit_closed_count += 1
                    det = evt.get("details") or {}
                    outage_sec = det.get("outage_sec") or det.get("outage_seconds")
                    if outage_sec is not None:
                        try:
                            outage_seconds_accumulated += float(outage_sec)
                        except Exception:
                            pass

            # 6. Conflict Check 冲突检查与智能降级
            if ename == EventName.CONFLICT_CHECK_STARTED:
                conflict_check_started_count += 1
            elif ename == EventName.CONFLICT_CHECK_SUCCEEDED:
                conflict_check_succeeded_count += 1
            elif ename == EventName.CONFLICT_CHECK_FAILED:
                conflict_check_failed_count += 1
            elif ename == EventName.CONFLICT_CHECK_DEGRADED:
                conflict_check_degraded_count += 1
            elif ename == EventName.LOCAL_CONFLICT_CHECK_STARTED:
                local_conflict_check_started_count += 1
            elif ename == EventName.LOCAL_CONFLICT_CHECK_SUCCEEDED:
                local_conflict_check_succeeded_count += 1
            elif ename == EventName.LOCAL_CONFLICT_DETECTED:
                local_conflict_detected_count += 1
            elif ename == EventName.LOCAL_CONFLICT_CHECK_BLOCKED:
                local_conflict_check_blocked_count += 1
            elif ename == EventName.CONFLICT_SERVICE_RECOVERED:
                conflict_service_recovered_count += 1

        unpaired_http_requests = len(started_request_ids - finished_request_ids)
        http_avg_duration_ms = round(sum(http_durations) / len(http_durations), 2) if http_durations else 0.0
        verification_avg_duration_ms = round(sum(verification_durations) / len(verification_durations), 2) if verification_durations else 0.0

        marker_degraded = False
        target_dir = log_dir or os.path.join(ROOT, "logs")
        if run_id and os.path.isdir(target_dir):
            marker_file = os.path.join(target_dir, f".degraded_{run_id}")
            if os.path.exists(marker_file):
                marker_degraded = True

        return {
            "total_events": len(deduped_events),
            "distinct_event_ids": len(seen_event_ids),
            "runs_count": len(runs_observed),
            "correlations_count": len(correlations_observed),
            # Candidate
            "candidate_count": candidate_count,
            "candidate_accepted": candidate_accepted,
            "candidate_rejected": candidate_rejected,
            "candidate_blocked": candidate_blocked,
            "candidate_reasons": dict(candidate_reasons),
            # Submission (真实提交)
            "submission_started_count": submission_started_count,
            "submission_succeeded_count": submission_succeeded_count,
            "submission_failed_count": submission_failed_count,
            "submission_unknown_count": submission_unknown_count,
            # Verification (复核，绝不计入 Submission)
            "verification_started_count": verification_started_count,
            "verification_confirmed_registered_count": verification_confirmed_registered_count,
            "verification_confirmed_unregistered_count": verification_confirmed_unregistered_count,
            "verification_remains_unknown_count": verification_remains_unknown_count,
            "verification_avg_duration_ms": verification_avg_duration_ms,
            # Reconciliation
            "reconciliation_started_count": reconciliation_started_count,
            "reconciliation_succeeded_count": reconciliation_succeeded_count,
            "reconciliation_failed_count": reconciliation_failed_count,
            # Infrastructure HTTP
            "http_requests_total": http_requests_total,
            "http_requests_succeeded": http_requests_succeeded,
            "http_requests_failed": http_requests_failed,
            "http_failures_by_code": dict(http_failures_by_code),
            "http_avg_duration_ms": http_avg_duration_ms,
            "timeout_count": http_failures_by_code.get(-1, 0),
            "network_error_count": sum(cnt for c, cnt in http_failures_by_code.items() if c < 0 or c >= 500),
            # Request ID & Audit Integrity
            "unpaired_http_requests": unpaired_http_requests,
            "audit_degraded": bool(unpaired_http_requests > 0 or marker_degraded),
            # Circuit breaker
            "circuit_trips": circuit_trips,
            "circuit_half_open_count": circuit_half_open_count,
            "circuit_closed_count": circuit_closed_count,
            "outage_seconds": round(outage_seconds_accumulated, 2),
            # Conflict Check (Phase 5)
            "conflict_check_started_count": conflict_check_started_count,
            "conflict_check_succeeded_count": conflict_check_succeeded_count,
            "conflict_check_failed_count": conflict_check_failed_count,
            "conflict_check_degraded_count": conflict_check_degraded_count,
            "local_conflict_check_started_count": local_conflict_check_started_count,
            "local_conflict_check_succeeded_count": local_conflict_check_succeeded_count,
            "local_conflict_detected_count": local_conflict_detected_count,
            "local_conflict_check_blocked_count": local_conflict_check_blocked_count,
            "conflict_service_recovered_count": conflict_service_recovered_count
        }
