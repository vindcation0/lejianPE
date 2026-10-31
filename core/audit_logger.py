"""
独立请求审计日志模块 (AuditLogger)
==================================
以 JSONL 格式记录每一次报名操作、复核操作及关键事件，实现操作全生命周期可追溯。
严格脱敏：禁止记录 AccessToken, RefreshToken, 手机号, 学号, 姓名。
"""
import datetime
import json
import os
import re
import uuid
from typing import Any, Dict, Optional


def sanitize_data(val: Any) -> Any:
    """递归脱敏敏感字段"""
    if isinstance(val, dict):
        sanitized = {}
        for k, v in val.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in ("token", "secret", "password", "auth")):
                sanitized[k] = "***MASKED***"
            elif any(s in k_lower for s in ("mobile", "phone", "student", "usercode", "usernumber", "realname", "identity")):
                sanitized[k] = "***MASKED***"
            else:
                sanitized[k] = sanitize_data(v)
        return sanitized
    elif isinstance(val, list):
        return [sanitize_data(item) for item in val]
    elif isinstance(val, str):
        # 针对字符串中可能的 Bearer token 或 11位手机号做脱敏
        val = re.sub(r"Bearer\s+[a-zA-Z0-9_\-\.]+", "Bearer ***MASKED***", val)
        val = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "1**********", val)
        return val
    return val


class AuditLogger:
    """审计日志记录器"""

    def __init__(self, logs_dir: str):
        self.logs_dir = os.path.abspath(logs_dir)
        os.makedirs(self.logs_dir, exist_ok=True)
        self._seq = 0

    def _get_request_path(self, date_str: str) -> str:
        return os.path.join(self.logs_dir, f"requests-{date_str}.jsonl")

    def _get_event_path(self, date_str: str) -> str:
        return os.path.join(self.logs_dir, f"events-{date_str}.jsonl")

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
        """记录单次提交/复核请求审计条目"""
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
            # 日志写入失败绝不应让程序失控，但必须控制台告警
            print(f"[警告] 写入请求审计日志失败: {e}")

        return record

    def log_event(self, event_type: str, details: Optional[Dict] = None):
        """记录系统状态事件（如列表恢复、熔断降级等）"""
        now = datetime.datetime.now()
        today_str = now.strftime("%Y%m%d")

        event = {
            "t": now.isoformat(timespec="seconds"),
            "event": event_type,
        }
        if details:
            event.update(sanitize_data(details))

        line = json.dumps(event, ensure_ascii=False)
        try:
            with open(self._get_event_path(today_str), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            print(f"[警告] 写入事件日志失败: {e}")
