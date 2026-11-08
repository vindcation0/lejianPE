# 第四轮优化 Phase 1 实施报告：修复活动列表请求与熔断器集成失效

**实施日期**：2026-11-08  
**分支**：`phase1/circuit-breaker-fix`（从 `main` @ `d91761e` 签出）  
**验证状态**：全部 287 项用例 100% 通过（基线 276 项 + 新增 11 项）

---

## 一、修改文件清单与修改原因

| 文件路径 | 修改性质 | 修改原因与核心逻辑 |
| :--- | :--- | :--- |
| `core/circuit_breaker.py` | 核心逻辑修复 | 1. **CLOSED 状态保护**：`record_success(is_complete=True)` 在 CLOSED 状态下，仅当 `is_complete=True` 时清零 `failure_count`；当接收到残缺列表（`is_complete=False`）时，保留已有失败计数，严禁归零。<br>2. **HALF_OPEN 状态收敛**：在 HALF_OPEN 状态下，无论是探测抛出异常/网络错误（进入 `record_failure`），还是探测返回残缺列表（`record_success(is_complete=False)`），均立即重新触发进入 OPEN（`_trip_to_open`），退避冷却时间递增，绝不允许在 HALF_OPEN 挂起。 |
| `lejian.py` | 调度与传播适配 | 1. **`ActivityList` 元数据扩展**：新增 `http_code` 属性与 `to_dict()` 导出，确保分页拉取失败时的具体 HTTP 状态码伴随列表对象向上传播。<br>2. **`Client.activities` 重试解耦**：新增 `max_retries=1` 可选参数（轮询主循环传入 1）。内部遇到非 200 或非 code=0 时记录 `last_http_code`，返回 `ActivityList(..., complete=False, http_code=last_http_code)`。<br>3. **抢课主循环故障准确归类**：当 `not getattr(items, "complete", True)` 时，判断 `is_server_fault(items.http_code)`：<br> - 若为基础设施故障（500/502/503/504/-1 超时），调用 `circuit_breaker.record_failure()`，累加失败计数并驱动熔断，同时由 `retry_policy` 指数退避；<br> - 若为客户端错误（401/403/400）或纯数据残缺，调用 `circuit_breaker.record_success(is_complete=False)`，在 CLOSED 下保留计数，在 HALF_OPEN 下重新 OPEN，避免客户端错误误熔断。 |
| `tests/test_circuit_breaker.py` | 测试套件扩充 | 新增 11 项强化单元与集成测试（C13～C23），全面覆盖 500/超时触发 OPEN、401/403 不熔断、HALF_OPEN 单次探测保护、内部重试防重复累计、熔断恢复等完整生命周期。 |

---

## 二、熔断器状态转换完整生命周期说明

```
       +------------------ is_server_fault 达到阈值 (3次) -------------------+
       |                                                                    |
       v                                                                    |
  +---------+   冷却时间到达 (cooldown_seconds)   +-----------+   探测成功且列表完整   +--------+
  |  OPEN   | ----------------------------------> | HALF_OPEN | -------------------> | CLOSED |
  +---------+                                     +-----------+                      +--------+
       ^                                                |                                 |
       |                   探测失败 或 数据不完整          |                                 |
       +------------------------------------------------+                                 |
       |                                                                                  |
       +---------------------------- 探测失败 / 异常 --------------------------------------+
```

1. **CLOSED 状态**：
   - 接收到完整列表（`is_complete=True`）：正常重置 `failure_count = 0`。
   - 接收到残缺列表（`is_complete=False`）：维持原有 `failure_count`，不归零。
   - 连续发生 3 次（阈值）服务端故障（500/502/503/504/网络超时）：计数累加达到 3，立即熔断切入 **OPEN** 状态。
   - 遇到 400/401/403 客户端异常：`is_server_fault` 返回 `False`，不计入基础设施故障，不增加失败计数。
2. **OPEN 状态**：
   - `can_attempt()` 返回 `(False, "熔断器处于 OPEN 状态...")`。
   - 外部主循环完全拦截正常列表轮询，避免对故障服务端持续施压。
   - 维持在 OPEN 状态直到指数退避冷却时间（默认 10s 起步，随失败次数指数倍增至 120s）耗尽。
3. **HALF_OPEN 状态**：
   - 冷却时间耗尽后，`can_attempt()` 仅允许**单次只读健康探测**（通过 `_half_open_probe_in_progress` 屏障锁定，阻止并发或多余请求）。
   - **分支 A（成功恢复）**：探测成功且活动列表完整（`is_complete=True`），熔断器平滑切回 **CLOSED** 状态，清零失败计数，后续正常轮询恢复。
   - **分支 B（探测失败）**：探测抛出网络异常或 HTTP 500 等故障，立即调用 `_trip_to_open()`，重新切回 **OPEN**，冷却时间递增。
   - **分支 C（列表残缺）**：探测即使 HTTP 200，但由于截断返回 `is_complete=False`，调用 `record_success(is_complete=False)`，由于无法证明业务健康，同样立即重新切回 **OPEN**，冷却时间递增。

---

## 三、内部重试与外层熔断的协作与防重复统计

在先前的实现中，`activities()` 内部自带 3 次内部快速重试（`for _ in range(3): time.sleep(0.25)`）。  
这导致两个严重问题：
1. **重试风暴与延迟叠加**：外层有 `poll` 间隔和 `retry_policy` 退避，若内部再重试 3 次，单次超时（如 6s）叠加内部重试将导致主循环挂起近 18 秒，无法及时响应。
2. **计数失真风险**：若直接将内部每次重试计入熔断器，单次轮询就会瞬间消耗全部 3 次失败阈值，导致一次抖动就误触发 OPEN。

**协同治理方案**：
- 在 `Client.activities` 中增加 `max_retries=1`（主抢课轮询时显式使用 1）。
- 内部快速重试与外部退避彻底解耦：外层单次轮询作为熔断器的**唯一定量观察窗口**（1 次失败轮询 = 1 次熔断失败事件）。
- 即使在其他脚本或菜单中以 `max_retries > 1` 调用 `activities`，外部熔断器也只以最终返回结果的 `http_code` 作为单次轮询的评估依据，绝不产生重复叠加统计。测试用例 `test_C22` 对此进行了专项 Mock 验证。

---

## 四、新增测试用例清单（11 项）

文件：`tests/test_circuit_breaker.py`（C13～C23）

| 用例 ID | 测试方法名 | 验证的核心契约与场景 |
| :--- | :--- | :--- |
| **C13** | `test_C13_closed_state_incomplete_list_does_not_clear_failure_count` | CLOSED 状态下遇到残缺列表（`complete=False`），严禁清零现有失败计数 |
| **C14** | `test_C14_activities_consecutive_three_500_trips_to_open` | 主轮询连续 3 次返回 HTTP 500 基础设施故障，准确触发进入 OPEN |
| **C15** | `test_C15_activities_consecutive_timeouts_trips_to_open` | 主轮询连续 3 次网络超时（HTTP -1），准确触发进入 OPEN |
| **C16** | `test_C16_gateway_errors_502_503_504_classified_as_server_fault` | 502/503/504 网关错误全部正确识别为基础设施故障并驱动熔断 |
| **C17** | `test_C17_activities_401_403_400_not_classified_as_server_fault` | 401（未授权）、403（拒绝访问）、400（参数错误）不计入基础设施故障，不触发熔断 |
| **C18** | `test_C18_open_state_blocks_normal_polling_until_cooldown` | OPEN 状态下严禁发起正常列表轮询，`can_attempt` 必须返回 False 并在冷却期内保持拦截 |
| **C19** | `test_C19_half_open_probe_success_with_complete_list_recovers_to_closed` | HALF_OPEN 单次健康探测成功且列表完整时，平滑恢复至 CLOSED 且清零失败计数 |
| **C20** | `test_C20_half_open_probe_failure_retrips_to_open` | HALF_OPEN 单次探测若发生故障（500），立即重新 OPEN 且退避冷却时间增加 |
| **C21** | `test_C21_half_open_incomplete_list_retrips_to_open` | HALF_OPEN 探测若返回残缺列表（`complete=False`），绝不能恢复业务，重新进入 OPEN |
| **C22** | `test_C22_activities_inner_retry_does_not_cause_duplicate_accumulation` | 验证 `activities` 即使存在内部重试，单次轮询也仅按 1 次失败计入熔断，杜绝重复累加 |
| **C23** | `test_C23_full_recovery_resumes_normal_polling` | 熔断恢复为 CLOSED 后，允许后续正常轮询连续成功并不被误拦截 |

---

## 五、全量测试执行结果与基线对比

### 1. 实际执行命令与结果

- **模块单测**：
  ```powershell
  python -m unittest tests.test_circuit_breaker
  ```
  输出：
  ```
  Ran 23 tests in 2.423s
  OK
  ```

- **全量回归套件**：
  ```powershell
  python -m tests.run_tests
  ```
  输出：
  ```
  Ran 287 tests in 20.783s
  OK
  ```

### 2. 与 Phase 0 基线对比

| 指标 | Phase 0 基线 | Phase 1 完成后 | 变动说明 |
| :--- | :--- | :--- | :--- |
| **测试总数** | 276 项 | **287 项** | 净增 11 项强化用例（C13～C23） |
| **通过率** | 100% (276/276) | **100% (287/287)** | 零回归，原有 276 项用例全部持续保持通过 |
| **执行耗时** | 19.385s | **20.783s** | 增加 ~1.4s（主要包含 11 项涉及短退避和状态转移的单测） |
| **熔断集成有效性** | **失效**（CLOSED 下被残缺列表清零；500 错误被 activities 吞噬无法驱动熔断） | **生效**（500/502/503/504/超时均能稳定驱动熔断；HALF_OPEN 严格校验完整性） | 核心可靠性问题彻底解决 |

---

## 六、尚存风险与后续建议

1. **Phase 2（UNKNOWN 安全屏障）待实施**：
   当前网络故障若发生在 `signUp` 提交过程中（而非列表拉取），仍需依赖 UNKNOWN 安全屏障。Phase 2 将针对状态持久化、启动自愈及队列熔断进行强化。
2. **Phase 3（日志与上报重构）待实施**：
   `AuditLogger` 与 `ResultReporter` 的统计接口需在后续阶段统一指标对齐。
3. **真实网络测试提示**：
   本次所有新增用例均采用标准 Mock 机制，严格未向实际生产环境提交任何报名写请求，保障环境无污染。
