"""
服务端故障熔断器模块 (CircuitBreaker)
=====================================
实现标准的 CLOSED / OPEN / HALF_OPEN 三态熔断器模式。
当服务端遭遇持续 5xx、超时、连接拒绝等基础设施故障时，主动停止无效的高频发包，
进入冷却期；冷却后通过单次只读健康检查安全探测，确认数据完整后平滑恢复业务。
"""
import random
import time
from typing import Dict, Optional, Tuple


class CircuitState:
    CLOSED = "CLOSED"        # 正常状态，允许所有请求
    OPEN = "OPEN"            # 熔断状态，禁止请求，进入冷却
    HALF_OPEN = "HALF_OPEN"  # 半开状态，仅允许单次只读健康探测


def is_server_fault(http_status: Optional[int] = None, error: Optional[Exception] = None) -> bool:
    """
    判断是否为可归因于服务端/网络故障的可恢复异常。
    - 5xx 状态码 (500, 502, 503, 504) -> True
    - HTTP -1 (超时) -> True
    - 超时、连接拒绝、网络错误异常 -> True
    - 4xx (如 400, 401, 403, 404) -> False (非服务端基础设施故障)
    - 正常 200 业务报错 -> False
    """
    if http_status is not None:
        if http_status == -1:
            return True
        if 500 <= http_status <= 599:
            return True
        if 400 <= http_status <= 499:
            return False

    if error is not None:
        err_str = str(error).lower()
        err_type = type(error).__name__.lower()
        fault_keywords = [
            "timeout", "timed out", "connection refused", "connection reset",
            "connection error", "connectionerror", "remotedisconnected",
            "network", "broken pipe", "getsockopt", "500", "502", "503", "504"
        ]
        if any(kw in err_str or kw in err_type for kw in fault_keywords):
            return True

    return False


class CircuitBreaker:
    """服务端故障熔断器"""

    def __init__(
        self,
        failure_threshold: int = 3,
        base_cooldown: float = 2.0,
        max_cooldown: float = 30.0,
        jitter_max: float = 0.5
    ):
        self.failure_threshold = failure_threshold
        self.base_cooldown = base_cooldown
        self.max_cooldown = max_cooldown
        self.jitter_max = jitter_max

        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.trip_count = 0
        self.opened_at = 0.0
        self.current_cooldown = base_cooldown
        self._half_open_probe_in_progress = False

        self.total_outage_seconds = 0.0
        self._last_open_start = 0.0

    def can_attempt(self) -> Tuple[bool, str]:
        """
        检查当前是否允许发起请求。
        返回: (allowed: bool, reason: str)
        """
        now = time.time()

        if self.state == CircuitState.CLOSED:
            return True, "CLOSED 正常运行"

        if self.state == CircuitState.OPEN:
            elapsed = now - self.opened_at
            if elapsed >= self.current_cooldown:
                # 冷却期结束，转换为 HALF_OPEN 进行单次探测
                self._transition_to_half_open()
                self._half_open_probe_in_progress = True
                return True, "HALF_OPEN 冷却完成，允许单次探测"
            else:
                remaining = self.current_cooldown - elapsed
                return False, f"OPEN 熔断冷却中，剩余 {remaining:.2f} 秒"

        if self.state == CircuitState.HALF_OPEN:
            if not self._half_open_probe_in_progress:
                self._half_open_probe_in_progress = True
                return True, "HALF_OPEN 允许单次探测"
            else:
                # C08: 半开状态只允许一次检查，无并发探测
                return False, "HALF_OPEN 正在进行单次健康探测，禁止并发请求"

        return False, f"未知熔断状态: {self.state}"

    def record_success(self, is_complete: bool = True) -> str:
        """
        记录请求成功。
        如果是 HALF_OPEN 探测：
        - is_complete 为 True 时，恢复 CLOSED；
        - is_complete 为 False 时（C09：列表残缺），重新 OPEN。
        """
        if self.state == CircuitState.HALF_OPEN:
            if is_complete:
                self._transition_to_closed()
            else:
                # C09: 返回不完整列表，不能恢复业务，重新进入 OPEN
                self._trip_to_open(reason="健康检查数据不完整 (complete=False)")
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0

        self._half_open_probe_in_progress = False
        return self.state

    def record_failure(
        self,
        http_status: Optional[int] = None,
        error: Optional[Exception] = None,
        force_trip: bool = False
    ) -> str:
        """
        记录请求失败。
        对故障进行严格分类，仅在服务端基础设施故障时累加失败计数并触发熔断。
        """
        if not force_trip and not is_server_fault(http_status=http_status, error=error):
            # 业务错误（如名额已满）或客户端错误不计入熔断
            return self.state

        self.failure_count += 1

        if self.state == CircuitState.HALF_OPEN:
            # C07: HALF_OPEN 探测失败，重新进入 OPEN
            self._trip_to_open(reason=f"HALF_OPEN 探测失败 (http={http_status}, err={error})")
        elif self.state == CircuitState.CLOSED:
            if force_trip or self.failure_count >= self.failure_threshold:
                # C02: 连续达到阈值，进入 OPEN
                self._trip_to_open(reason=f"连续故障达到阈值 ({self.failure_count}/{self.failure_threshold})")

        self._half_open_probe_in_progress = False
        return self.state

    def _trip_to_open(self, reason: str = ""):
        """进入熔断状态 OPEN，按指数退避计算冷却时间"""
        self.state = CircuitState.OPEN
        self.trip_count += 1
        now = time.time()
        self.opened_at = now
        self._last_open_start = now
        self._half_open_probe_in_progress = False

        # 指数退避：base * (2 ^ (trip_count - 1)) + jitter
        exp = min(self.trip_count - 1, 6)
        backoff = min(self.max_cooldown, self.base_cooldown * (2 ** exp))
        jitter = random.uniform(0, self.jitter_max)
        self.current_cooldown = round(backoff + jitter, 2)

    def _transition_to_half_open(self):
        """进入半开状态 HALF_OPEN"""
        now = time.time()
        if self._last_open_start > 0:
            self.total_outage_seconds += (now - self._last_open_start)
            self._last_open_start = 0.0
        self.state = CircuitState.HALF_OPEN
        self._half_open_probe_in_progress = False

    def _transition_to_closed(self):
        """恢复正常状态 CLOSED"""
        now = time.time()
        if self._last_open_start > 0:
            self.total_outage_seconds += (now - self._last_open_start)
            self._last_open_start = 0.0
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self._half_open_probe_in_progress = False

    def get_remaining_cooldown(self) -> float:
        """获取当前 OPEN 状态剩余冷却秒数"""
        if self.state != CircuitState.OPEN:
            return 0.0
        elapsed = time.time() - self.opened_at
        return max(0.0, round(self.current_cooldown - elapsed, 2))

    def get_metrics(self) -> Dict:
        """获取熔断器运行指标字典"""
        outage = self.total_outage_seconds
        if self.state == CircuitState.OPEN and self._last_open_start > 0:
            outage += (time.time() - self._last_open_start)
        return {
            "state": self.state,
            "failure_count": self.failure_count,
            "trip_count": self.trip_count,
            "current_cooldown": self.current_cooldown,
            "remaining_cooldown": self.get_remaining_cooldown(),
            "total_outage_seconds": round(outage, 2)
        }
