"""
重试与指数退避策略模块 (RetryPolicy)
=====================================
管理查询请求的指数退避与系统运行健康状态 (NORMAL, DEGRADED, RECOVERING)。
服务端异常或限流时自动降低轮询频率，恢复后渐进式恢复。
"""
import random
import time
from typing import Optional


class HealthState:
    NORMAL = "NORMAL"          # 接口工作正常
    DEGRADED = "DEGRADED"      # 连续 5xx 或超时，进入降级退避
    RECOVERING = "RECOVERING"  # 接口恢复，正在核对状态


class RetryPolicy:
    """退避与健康状态管理器"""

    def __init__(
        self,
        base_delay: float = 0.5,
        max_delay: float = 8.0,
        degraded_threshold: int = 2,
        jitter_max: float = 0.25
    ):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.degraded_threshold = degraded_threshold
        self.jitter_max = jitter_max

        self.consecutive_failures = 0
        self.state = HealthState.NORMAL
        self._last_state_change = time.time()

    def record_success(self) -> str:
        """记录查询成功，更新健康状态"""
        prev_state = self.state
        if self.state == HealthState.DEGRADED:
            # 刚从降级恢复，进入 RECOVERING 进行状态复核
            self.state = HealthState.RECOVERING
            self._last_state_change = time.time()
        elif self.state == HealthState.RECOVERING:
            # 保持或由外部显式转为 NORMAL
            pass
        else:
            self.state = HealthState.NORMAL

        self.consecutive_failures = 0
        return self.state

    def record_failure(self, http_status: Optional[int] = None) -> str:
        """记录查询失败，更新失败计数和健康状态"""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.degraded_threshold:
            self.state = HealthState.DEGRADED
            self._last_state_change = time.time()
        return self.state

    def mark_recovered(self):
        """外部复核完成后，由 RECOVERING 转为 NORMAL"""
        self.state = HealthState.NORMAL
        self.consecutive_failures = 0
        self._last_state_change = time.time()

    def get_backoff_delay(self, retry_after: Optional[float] = None) -> float:
        """
        计算下一次重试等待秒数。
        如果服务端返回了 Retry-After (429)，优先遵守服务端要求。
        """
        if retry_after is not None and retry_after > 0:
            return float(retry_after)

        if self.consecutive_failures <= 0:
            return 0.0

        exp = min(self.consecutive_failures - 1, 6)
        delay = min(self.max_delay, self.base_delay * (2 ** exp))
        jitter = random.uniform(0, self.jitter_max)
        return round(delay + jitter, 3)

    def sleep_backoff(self, retry_after: Optional[float] = None) -> float:
        """执行退避等待并返回等待时间"""
        d = self.get_backoff_delay(retry_after)
        if d > 0:
            time.sleep(d)
        return d
