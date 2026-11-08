# 第四轮优化 Phase 2 实施报告：强化 UNKNOWN 安全屏障

**实施日期**：2026-11-08  
**当前分支**：`phase2/unknown-barrier-hardening`（基于 `phase1/circuit-breaker-fix` @ `d3f7a21` 签出）  
**验证状态**：全部 302 项测试 100% 通过（基线 276 项 + Phase 1 18 项 + Phase 2 8 项）

---

## 一、修改文件清单与关键设计

| 文件路径 | 修改性质 | 核心修改原因与保护机制 |
| :--- | :--- | :--- |
| `core/request_manager.py` | 核心判定加固 | **`verify_registration` 严密完整性校验**：<br>1. 必须在 `http_code == 200` 或无 HTTP 异常下执行；<br>2. 确认未报名（`isRegister=False`）必须满足**列表数据完全完整（`complete == True`）**；若列表由于分页网络故障被截断（`complete=False`），严禁轻率断定为未报名，返回 `(False, None, None)` 保持未知；<br>3. 目标活动未在列表中检索到（下架/不可见/未盖全）时，严禁断定为未报名，返回 `(False, None, None)` 保持未知；<br>4. 列表返回为空或数据结构异常时，返回 `(False, None, None)`。 |
| `core/recovery_manager.py` | 启动自愈增强 | **`recover_on_startup` 元数据附带**：<br>在启动只读复核确认成功的记录中，附带 `remote_item` 完整活动元数据（含 `startTime` 等），为后续配额与课表无缝衔接提供完整上下文。 |
| `lejian.py` | 调度与配额防重 | 1. **元数据降级查询**：未决活动复核成功后，按 `remote_item -> items 查找 -> target_unres` 三级回退提取活动详情；<br>2. **配额防重复累加**：在增加 `week_done` 与 `DAY_DONE` 之前，检查 `already_in_items`，若当前 `items` 中已标记为 `isRegister=True`，不再重复累加本地运行增量，彻底杜绝跨轮询将单节课程误计为双倍名额占用。 |
| `tests/test_unknown_barrier.py` | 测试套件扩充 | 新增 8 项针对性强化单测（`test_U13` 至 `test_U20`），全面覆盖熔断器解耦、残缺列表防御、目标缺失防御、网关 502 防御、崩溃遗留恢复与配额上限闭环。 |

---

## 二、Phase 2 核心机制强化说明

### 1. 熔断器与 UNKNOWN 彻底解耦独立
- **机制保证**：
  ```
  UNKNOWN 状态未决
         │
         ▼
  服务端发生故障
         │
         ▼
  熔断器进入 OPEN
         │
         ▼
  冷却期结束，单次探测成功，熔断器恢复 CLOSED
         │
         ▼
  UNKNOWN 依然存在于 StateStore
         │
         ▼
  候选队列继续保持严格冻结，禁止任何新候选提交
  ```
- **核心契约**：熔断器仅反映“底层 HTTP 网络与接口连通性”，而 UNKNOWN 反映“具体活动的提交事务是否已在服务端落库”。网络恢复绝不代表业务已确认，只要 SQLite 中存在处于 `SUBMITTING / UNKNOWN / VERIFYING` 的记录，`register_activity()` 与主循环均坚决阻断新候选提交。用例 `test_U13` 对此进行了严格的断言验证。

### 2. 状态复核完整性校验四要素
判定未报名必须同时满足：
1. HTTP 状态码为 200（无 500/502/503/504/超时）；
2. 列表分页数据完整（`complete == True`）；
3. 在完整列表中检索到了目标活动的 `activity_id`；
4. 该目标活动的数据对象中明确读取到 `isRegister == False`。

任一要素不满足（如 502、列表截断、活动不在列表中、列表为空），均返回 `(False, None, None)`，严格维持 `UNKNOWN`。

### 3. 崩溃恢复（Crash Recovery）安全语义
针对 `SUBMITTING` 阶段突发崩溃：
- 重启扫描发现 `SUBMITTING` 遗留记录；
- 转换为 `VERIFYING`，发起**只读** `verify_registration()` 查询；
- **绝不重新调用 `signUp` 提交网络包**；
- 若服务端确认已报，转为 `SUCCESS`；
- 若服务端确认未报，转为 `RETRYABLE`；
- 若网络超时或 500 导致无法核实，安全置为 `UNKNOWN`，继续冻结候选队列，等待后续轮询自愈。

### 4. 配额与上限闭环
- UNKNOWN 复核为 `SUCCESS` 后，立即获取活动的 `startTime`，更新当周普通活动数量（`week_done`）、当天活动数量（`DAY_DONE`）或特殊活动数量（`special_done`）；
- 防重保护：若服务端返回的 `items` 已经包含了该活动且标记了 `isRegister=True`，避免 `reg_by_week + week_done` 产生二次累加；
- 若该周或该日已达上限（`max_normal` / `max_per_day`），后续同周/同日候选活动将自动被规则拦截器（`validate_rules`）安全阻断，杜绝超额报名。

---

## 三、新增测试用例清单（8 项）

文件：[`tests/test_unknown_barrier.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/tests/test_unknown_barrier.py)（U13～U20）

| 用例 ID | 测试方法名 | 验证的核心契约与场景 |
| :--- | :--- | :--- |
| **U13** | `test_U13_circuit_breaker_recovery_to_closed_does_not_clear_unknown_barrier` | 熔断器恢复至 CLOSED 绝不解除 UNKNOWN 屏障，未决记录存在时必须继续冻结候选队列 |
| **U14** | `test_U14_unknown_verify_not_registered_requires_complete_list` | 复核确认未报名必须满足列表完整性明确为 True，残缺列表（complete=False）禁止判定为未报名 |
| **U15** | `test_U15_unknown_verify_target_missing_from_list_keeps_frozen` | 复核时目标活动未在列表中检索到（可能下架或分页未盖全），禁止判定为未报名，保持 UNKNOWN |
| **U16** | `test_U16_unknown_verify_server_fault_502_keeps_frozen` | 复核请求遇到 HTTP 502/500 网关错误，严格保持 UNKNOWN 并冻结 |
| **U17** | `test_U17_unknown_verify_empty_or_malformed_list_keeps_frozen` | 复核列表返回空列表或格式异常，禁止判定为未报名，保持 UNKNOWN |
| **U18** | `test_U18_crash_recovery_submitting_verified_success_never_resubmits` | 崩溃重启恢复遗留 SUBMITTING：只读复核为 SUCCESS 时更新状态且绝不再次发起网络提交 |
| **U19** | `test_U19_crash_recovery_submitting_verified_failure_recovers_to_unknown` | 崩溃重启恢复遗留 SUBMITTING：若复核遇网络异常，安全置为 UNKNOWN 并冻结队列，禁止重新提交 |
| **U20** | `test_U20_unknown_confirmed_success_quota_update_blocks_further_candidates` | UNKNOWN 复核确认成功后更新配额，若达到配额上限则阻止后续同周候选提交 |

---

## 四、全量测试执行与基线对比

```powershell
# 1. UNKNOWN 安全屏障专项测试（20 项全部通过）
python -m unittest tests.test_unknown_barrier
# 输出：Ran 20 tests in 0.680s -> OK

# 2. 全量回归测试套件（302 项全部通过，零回归）
python -m tests.run_tests
# 输出：Ran 302 tests in 23.554s -> OK
```

| 阶段 | 测试总数 | 通过数 | 失败数 | 通过率 | 覆盖主要模块 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Phase 0 基线** | 276 | 276 | 0 | 100% | 基础认证、课表、持久化 |
| **Phase 1 结束** | 294 | 294 | 0 | 100% | + 熔断器集成、重试语义规范 |
| **Phase 2 结束** | **302** | **302** | **0** | **100%** | + UNKNOWN 屏障独立、完整性防御、崩溃恢复、配额更新 |
