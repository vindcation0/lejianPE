"""
HTTP 请求管理与分类模块 (RequestManager)
======================================
将 HTTP 请求严格区分为：
1. 查询请求 (request_query): 无副作用，失败时按策略退避重试，支持 401 自动刷新。
2. 提交请求 (submit_once): 有实际副作用，绝不自动重试，超时/502/503 判定为 UNKNOWN。
3. 状态复核 (verify_registration): 通过 getActivityList 检查 target activity 的 isRegister。
"""
import datetime
import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


class SubmitResult:
    """提交结果结构化封装"""

    def __init__(
        self,
        http_status: int,
        raw_text: str = "",
        json_data: Optional[Dict] = None,
        classification: str = "UNKNOWN",
        reason: str = "",
        error: Optional[str] = None
    ):
        self.http_status = http_status
        self.raw_text = raw_text
        self.json_data = json_data or {}
        self.classification = classification  # SUCCESS, FAILED, UNKNOWN, UNAUTHORIZED, RATE_LIMITED
        self.reason = reason
        self.error = error

    def __repr__(self):
        return f"<SubmitResult status={self.http_status} class={self.classification} reason={self.reason!r}>"


class RequestManager:
    """请求管理器"""

    def __init__(self, client: Any, retry_policy: Any = None):
        self.client = client
        self.retry_policy = retry_policy

    def request_query(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
        timeout: int = 6,
        max_retries: int = 3
    ) -> Tuple[int, Any]:
        """
        发起查询请求。遇到 5xx 或超时进行有限次退避重试。
        遇到 401 自动刷新 token 并重试一次。
        """
        retries = 0
        refreshed = False

        while retries <= max_retries:
            retries += 1
            code, data = self.client.call(method, path, body=body if method == "POST" else params, timeout=timeout)

            # 401 鉴权失效
            if code == 401 and not refreshed:
                try:
                    self.client.refresh()
                    refreshed = True
                    continue
                except Exception as e:
                    if self.retry_policy:
                        self.retry_policy.record_failure(code)
                    return code, {"_raw": f"Token刷新失败: {e}"}

            # 429 限流
            if code == 429:
                if self.retry_policy:
                    self.retry_policy.record_failure(code)
                    self.retry_policy.sleep_backoff(retry_after=2.0)
                else:
                    time.sleep(1.0)
                continue

            # 正常返回
            if code == 200 and isinstance(data, dict):
                if self.retry_policy:
                    self.retry_policy.record_success()
                return code, data

            # 服务端 5xx 或超时（code == -1）
            if code >= 500 or code == -1:
                if self.retry_policy:
                    self.retry_policy.record_failure(code)
                    if retries <= max_retries:
                        self.retry_policy.sleep_backoff()
                else:
                    if retries <= max_retries:
                        time.sleep(0.3)
                continue

            # 其他状态码直接返回
            if self.retry_policy:
                self.retry_policy.record_failure(code)
            return code, data

        return code, data

    def submit_once(
        self,
        path: str,
        body: Dict,
        timeout: int = 6
    ) -> SubmitResult:
        """
        提交有副作用的请求（如 signUp）。
        绝对不自动重复提交！
        根据响应将结果严格分类为 SUCCESS / FAILED / UNKNOWN / UNAUTHORIZED / RATE_LIMITED。
        """
        try:
            code, data = self.client.call("POST", path, body=body, timeout=timeout)
        except Exception as e:
            code = -1
            data = str(e)
        raw_text = data.get("_raw", "") if isinstance(data, dict) else str(data)

        # 1. 网络超时或严重异常
        if code == -1:
            return SubmitResult(
                http_status=code,
                raw_text=raw_text,
                json_data=data if isinstance(data, dict) else None,
                classification="UNKNOWN",
                reason="请求超时（结果未知）",
                error=raw_text or "Timeout"
            )

        # 2. 网关错误 502 / 503 / 500
        if code in (500, 502, 503, 504):
            return SubmitResult(
                http_status=code,
                raw_text=raw_text,
                json_data=data if isinstance(data, dict) else None,
                classification="UNKNOWN",
                reason=f"网关或服务异常 HTTP {code}（结果未知）",
                error=raw_text
            )

        # 3. 401 未授权
        if code == 401:
            return SubmitResult(
                http_status=code,
                raw_text=raw_text,
                json_data=data if isinstance(data, dict) else None,
                classification="UNAUTHORIZED",
                reason="Token已过期",
                error="Unauthorized"
            )

        # 4. 429 被限流
        if code == 429:
            return SubmitResult(
                http_status=code,
                raw_text=raw_text,
                json_data=data if isinstance(data, dict) else None,
                classification="RATE_LIMITED",
                reason="请求触发服务端限流",
                error="Too Many Requests"
            )

        # 5. HTTP 200 业务判断
        if code == 200 and isinstance(data, dict):
            b_code = data.get("code")
            b_data = data.get("data")
            msg = str(data.get("message") or "")

            if b_code == 0:
                if isinstance(b_data, dict):
                    if b_data.get("success") is True:
                        return SubmitResult(
                            http_status=code,
                            raw_text=raw_text,
                            json_data=data,
                            classification="SUCCESS",
                            reason="报名成功"
                        )
                    elif b_data.get("success") is False:
                        fail_reason = b_data.get("reason") or "报名失败"
                        return SubmitResult(
                            http_status=code,
                            raw_text=raw_text,
                            json_data=data,
                            classification="FAILED",
                            reason=fail_reason
                        )
                # data 为 True 或非空成功的其他情形
                if b_data is True:
                    return SubmitResult(
                        http_status=code,
                        raw_text=raw_text,
                        json_data=data,
                        classification="SUCCESS",
                        reason="报名成功"
                    )

            # code != 0 的明确业务拒绝
            fail_keywords = ["上限", "名额已满", "未在", "时间", "冲突", "不允许", "重复"]
            if any(k in msg for k in fail_keywords):
                return SubmitResult(
                    http_status=code,
                    raw_text=raw_text,
                    json_data=data,
                    classification="FAILED",
                    reason=msg or f"业务拒绝 (code={b_code})"
                )

            # 无法解析的 code != 0
            return SubmitResult(
                http_status=code,
                raw_text=raw_text,
                json_data=data,
                classification="UNKNOWN",
                reason=f"未明确业务响应: {msg} (code={b_code})"
            )

        # 非 200 且非已知错误
        return SubmitResult(
            http_status=code,
            raw_text=raw_text,
            json_data=data if isinstance(data, dict) else None,
            classification="UNKNOWN",
            reason=f"未预期状态码 HTTP {code}",
            error=raw_text
        )

    def verify_registration(
        self,
        activity_id: str,
        timeout: int = 6
    ) -> Tuple[bool, Optional[bool], Optional[Dict]]:
        """
        通过查询接口获取活动列表，核实 target activity 是否已报名 (isRegister)。
        返回: (verified: bool, is_registered: Optional[bool], item: Optional[dict])

        严格判定规则：
        1. 确认已报名 (True, True, item)：
           - 列表中检索到目标活动，且 isRegister 为 True。
        2. 确认未报名 (True, False, item)：
           - 列表拉取成功（HTTP 200，无网络异常）；
           - 分页数据完整（complete 明确为 True）；
           - 列表中检索到目标活动，且 isRegister 明确为 False。
        3. 无法确认 (False, None, None)：
           - 网络超时、连接失败或 HTTP 5xx；
           - 列表分页失败或数据不完整 (complete 为 False 或未知)；
           - 列表中未检索到目标活动（可能下架、不可见或分页截断）；
           - 返回数据结构异常或为空。
        """
        try:
            # 使用 client 的 activities 拉取最新列表（默认 max_retries=0，单次请求无隐式重试）
            items = self.client.activities(page_size=200, req_timeout=timeout)
        except Exception:
            return False, None, None

        if not items or not isinstance(items, list):
            return False, None, None

        # 检查 HTTP 状态码（若存在非 200 则直接判定未确认）
        http_code = getattr(items, "http_code", None)
        if http_code is not None and http_code != 200:
            return False, None, None

        # 检查分页完整性标记（若是 ActivityList 且 complete=False 则为残缺）
        is_complete = bool(getattr(items, "complete", True))

        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get("id")) == str(activity_id):
                is_reg = bool(it.get("isRegister"))
                if is_reg:
                    # 只要明确确认已在服务端报名，即使分页后续未完也能确认成功
                    return True, True, it
                # 确认未报名必须满足：数据完全完整且无截断
                if not is_complete:
                    return False, None, None
                return True, False, it

        # 列表中未检索到该活动（数据不全或已下架），严禁轻率断定为未报名
        return False, None, None
