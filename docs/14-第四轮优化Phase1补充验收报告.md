# 第四轮优化 Phase 1 补充验收报告：熔断器边界条件与重试语义核查

**验收日期**：2026-11-08  
**当前分支**：`phase1/circuit-breaker-fix`  
**验证状态**：全部 294 项测试 100% 通过（基线 276 项 + Phase 1 初始 11 项 + 补充验收 7 项）

---

## 一、两个核心问题的排查结论与说明

### 任务一：HALF_OPEN 状态下的客户端异常

#### 1. 问题核查
- **排查前状态**：
  在 Phase 1 初始实现中，`CircuitBreaker.record_failure()` 中判断 `if self.state == CircuitState.HALF_OPEN:` 位于 `is_server_fault()` 之前；此外，在 `_cmd_grab_inner` 中，返回 401/403/400 时被归入 `else` 分支调用了 `circuit_breaker.record_success(is_complete=False)`。这会导致半开探测遇到 401（Token 失效）时，被误判为探测失败或不完整列表而调用 `_trip_to_open()`，使 `trip_count` 递增、退避冷却时间呈指数级恶化（最高达 30s~120s）。
- **优化修复措施**：
  1. 在 `core/circuit_breaker.py` 的 `record_failure()` 中，**优先检查 `is_server_fault()`**。401、403、400 属于 4xx 客户端异常，直接命中 `not is_server_fault` 分支。
  2. 在 HALF_OPEN 遇到客户端异常时，**安全释放单次探测锁（`_half_open_probe_in_progress = False`）**，且**状态维持在 `HALF_OPEN`**，不递增 `trip_count`，不递增 `current_cooldown`，不增加 `failure_count`。
  3. 在 `_cmd_grab_inner` 中细化错误分支：
     - **401**：记录客户端异常，释放探测锁，立即触发 `c.refresh()` 刷新凭据。下一次循环时由于处于 HALF_OPEN 且锁已释放，立即发起健康探测并以新凭据重新验证完整列表。
     - **403**：记录客户端异常，输出明确的权限日志提示（`[权限异常] 活动列表返回 HTTP 403 (禁止访问/权限不足)`）。
     - **400**：记录客户端异常，输出参数异常提示。
  4. 恢复探测必须重新取得完整列表（`is_complete=True`）才能恢复 `CLOSED`，绝不因客户端错误而误恢复。

---

### 任务二：核实 max_retries 语义

#### 1. 问题核查回答

| 关注点 | 修复前行为 | 规范后行为（准确语义） |
| :--- | :--- | :--- |
| **`max_retries=1` 实际发起几次 HTTP 请求？** | 实际发起 **1 次**（循环 `for _ in range(max_retries)` 仅执行 1 次，即只有 1 次首次请求，0 次重试，与参数名严重歧义） | 明确重试语义：**最多 2 次**（1 次首次请求 + 1 次额外重试） |
| **`max_retries=0` 是否允许？** | **不允许**。`range(0)` 为空，导致循环体一次都不执行，未发起任何请求便直接抛出错误 | **完全允许**。表示“不进行额外重试，仅发起 1 次首次请求”，总共发起 **1 次** 底层 HTTP 请求 |
| **主轮询实际使用几次底层请求？** | 修复前传入 1 误打误撞跑了 1 次，但语义混乱 | 显式传入 `max_retries=0`（默认值亦为 0），单次轮询在失败时**严格只发起 1 次**底层 HTTP 请求 |
| **内部重试是否会导致请求次数和熔断统计不一致？** | 会。若开启内部重试（如尝试 3 次），底层产生 3 个请求包，但外层仅计 1 次失败，且单次轮询在超时时会挂起 18 秒 | 主轮询统一固定为 `max_retries=0`，使底层 HTTP 请求、外层轮询与熔断器采样严格 **1:1:1 对齐**，彻底杜绝统计失真与重试风暴 |

#### 2. 代码参数规范
在 [`lejian.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/lejian.py) 的 `Client.activities()` 中增加规范注释与支持：
```python
def activities(self, page_size=200, req_timeout=6, max_retries=0, max_attempts=None):
    """
    参数语义规范:
    - max_retries: 失败后的额外重试次数。默认 0（即仅首次请求 1 次，不进行额外重试）。
      - max_retries=0: 允许，总共发起 1 次 HTTP 请求（1 首次 + 0 重试）。
      - max_retries=1: 允许，总共最多发起 2 次 HTTP 请求（1 首次 + 1 重试）。
    - max_attempts: 总尝试次数别名（若显式指定则优先使用：total_attempts = max(1, max_attempts)）。
    """
    if max_attempts is not None:
        total_attempts = max(1, int(max_attempts))
    else:
        total_attempts = 1 + max(0, int(max_retries))
```

---

## 二、新增补充验收测试清单（7 项）

文件：[`tests/test_circuit_breaker.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/tests/test_circuit_breaker.py)（C24～C30）

| 用例 ID | 测试方法名 | 验证的核心契约与场景 |
| :--- | :--- | :--- |
| **C24** | `test_C24_half_open_http_401_releases_probe_lock_and_preserves_cooldown` | HALF_OPEN 遇到 401：严禁计入基础设施故障，释放探测锁，保持退避时间与 trip_count 不变，允许立即再次探测 |
| **C25** | `test_C25_half_open_http_403_and_400_not_classified_as_server_fault` | HALF_OPEN 遇到 403 / 400：释放探测锁，不增加故障计数，不增加退避 |
| **C26** | `test_C26_half_open_401_recovers_to_closed_after_reauth_probe` | HALF_OPEN 遇到 401 后完成重认证，再次健康探测成功且列表完整时平滑恢复 CLOSED |
| **C27** | `test_C27_client_errors_do_not_cause_infinite_backoff` | 连续 10 次客户端异常（401/403/400）绝不造成 `trip_count` 累加或冷却时间无限指数膨胀 |
| **C28** | `test_C28_activities_max_retries_zero_executes_single_http_call` | `activities(max_retries=0)` 允许且仅发起 1 次底层 HTTP 请求（0 次额外重试） |
| **C29** | `test_C29_activities_max_retries_one_executes_two_http_calls_on_failure` | `activities(max_retries=1)` 失败时发起 2 次底层 HTTP 请求（1 首次 + 1 重试） |
| **C30** | `test_C30_main_grab_polling_mock_http_call_count_exact_match` | 主抢课轮询单次轮询在故障时底层 HTTP 调用次数严格为 1，与熔断统计严格 1:1 对齐，检查 Mock HTTP 调用计数 |

---

## 三、全量测试执行结果对比

```powershell
# 1. 熔断器专项模块测试
python -m unittest tests.test_circuit_breaker
# 输出：Ran 30 tests in 2.909s -> OK

# 2. 全量回归测试套件
python -m tests.run_tests
# 输出：Ran 294 tests in 7.618s -> OK
```

| 阶段 | 测试总数 | 通过数 | 失败数 | 通过率 |
| :--- | :--- | :--- | :--- | :--- |
| **Phase 0 基线** | 276 | 276 | 0 | 100% |
| **Phase 1 初始交付** | 287 | 287 | 0 | 100% |
| **Phase 1 补充验收** | **294** | **294** | **0** | **100%** |
