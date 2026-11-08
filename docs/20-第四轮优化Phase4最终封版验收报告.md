# 乐健体育抢课系统 - Phase 4 最终封版：结论判定与数据完整性补充验收报告

## 一、概述

本报告记录乐健体育抢课系统第四轮优化 **Phase 4 最终封版：结论判定与数据完整性补充验收** 的实施细节与完整回归验证结果。

- **工作分支**：`phase4/result-reporter-refactor`
- **封版基准**：基于 `cc1544b` 进行补充验收与边界修复
- **封版原则**：
  1. **修正 RETRYABLE 与 UNKNOWN 的语义边界**：明确区分 `uncertain_count` (SUBMITTING, UNKNOWN, VERIFYING) 与 `pending_retry_count` (RETRYABLE)。`uncertain_count > 0` 强制 `UNVERIFIED`；仅存在 `pending_retry_count > 0` 时禁止虚假 `SUCCESS`，但不产生虚假 `UNVERIFIED`，根据落地情况输出 `PARTIAL_SUCCESS` 或 `FAILED`。
  2. **严格要求最终对账显式为 SUCCESS**：只有 `reconcile_status == "SUCCESS"` 且列表完整 (`complete=True`)，且与当前运行的 `run_id` 匹配时，才能进入成功判定；`None`、缺失、对账未执行、对账失败或残缺一律输出 `UNVERIFIED`。
  3. **审计降级持久化与恢复**：在写入失败时持久化降级标记 `.degraded_{run_id}`，配合未配对 HTTP 事件回放分析，禁止异常退出重启后将上一轮破损日志宣称为完整。
  4. **全局对账解除 UNKNOWN 状态一致性**：最终对账成功且列表完整时，自动识别服务端已报名的 UNKNOWN 活动并同步更新 StateStore 为 `SUCCESS`，避免重复累计或未决冻结滞留；列表残缺时严禁解除。
  5. **保留全部安全屏障与历史机制**：不修改 Phase 1 熔断器、不修改 Phase 2 多进程锁与原子预留、不改变单次提交机制，全量 359 项测试 100% 通过。

---

## 二、修改文件清单

| 文件路径 | 修改性质 | 主要改动说明 |
| :--- | :--- | :--- |
| `core/result_reporter.py` | 核心强化 | 1. 拆分 `uncertain_count` (SUBMITTING/UNKNOWN/VERIFYING) 与 `pending_retry_count` (RETRYABLE)。<br>2. 严格化 `compute_final_verdict()`：`reconcile_status != "SUCCESS"` 或缺失/为 `None`、未执行、或跨 `run_id` 污染时强制判定 `UNVERIFIED`；`pending_retry_count > 0` 判定为 `PARTIAL_SUCCESS`（已有落地）或 `FAILED`（无落地）。<br>3. `generate_summary()` 显式输出 `uncertain_count` 与 `pending_retry_count`。 |
| `core/reconciliation.py` | 状态同步 | 1. `ReconciliationManager.__init__` 支持注入 `state_store`。<br>2. `reconcile()` 返回报表携带 `run_id`、`status` 与 `complete` 标志。<br>3. 全局对账成功且列表完整时，遍历 `state_store.get_unresolved_records()`：在列表中确认已报者解除为 `SUCCESS`，确认未报者解除为 `RETRYABLE`，并产生 `verification` 审计事件；列表残缺时严禁解除。 |
| `core/audit_logger.py` | 降级持久化 | 1. `record_event` 与 `log_request` 发生写异常时，调用 `_mark_run_degraded(run_id)` 持久化 `.degraded_{run_id}` 标记。<br>2. `AuditLogReader.replay()` 支持跨会话读取降级标记，确保进程崩溃重启后不会误判旧日志完整。 |
| `lejian.py` | 链路组装 | 抢课收尾阶段初始化 `ReconciliationManager` 时传入 `state_store=state_store`，激活对账自动状态同步。 |
| `tests/test_report_consistency.py` | 补充验收测试 | 新增 R16–R25 共 10 项严密边界测试，覆盖语义边界、对账合法性、降级检测与 UNKNOWN 状态一致性。 |

---

## 三、最终结论判定树（Decision Tree）

```mermaid
flowchart TD
    Start([输入对账与状态数据]) --> C_Uncertain{存在不确定状态？<br>uncertain_count > 0<br>(SUBMITTING / UNKNOWN / VERIFYING)}
    C_Uncertain -- 是 --> R_UNVERIFIED["UNVERIFIED<br>（存在无法确认的报名请求，安全屏障生效）"]
    
    C_Uncertain -- 否 --> C_ReconcilePerformed{是否执行过对账？<br>reconciliation_performed == True}
    C_ReconcilePerformed -- 否 --> R_UNVERIFIED
    
    C_ReconcilePerformed -- 是 --> C_ReconcileStatus{对账是否明确成功？<br>reconcile_status == 'SUCCESS'<br>AND complete == True}
    C_ReconcileStatus -- 否 --> R_UNVERIFIED
    
    C_ReconcileStatus -- 是 --> C_RunIdMatch{对账报告 run_id 与当前运行匹配？<br>report.run_id == reporter.run_id}
    C_RunIdMatch -- 否 --> R_UNVERIFIED
    
    C_RunIdMatch -- 是 --> C_AuditDegraded{审计日志严重降级？<br>unpaired_http_requests > 0}
    C_AuditDegraded -- 是 --> R_UNVERIFIED
    
    C_AuditDegraded -- 否 --> C_SuccessCount{服务端确认成功数<br>reconciled_success > 0 ?}
    
    C_SuccessCount -- 是 --> C_Limits{存在超额报名？<br>daily / weekly exceeded}
    C_Limits -- 是 --> R_PARTIAL["PARTIAL_SUCCESS<br>（已超额，需人工退课）"]
    C_Limits -- 否 --> C_PendingRetry{存在未完成的重试目标？<br>pending_retry_count > 0 (RETRYABLE)}
    C_PendingRetry -- 是 --> R_PARTIAL["PARTIAL_SUCCESS<br>（部分成功，存在 RETRYABLE 未完成）"]
    C_PendingRetry -- 否 --> C_ExplicitFailed{存在明确失败或被拒？<br>explicit_failed > 0}
    C_ExplicitFailed -- 是 --> R_PARTIAL["PARTIAL_SUCCESS<br>（部分成功，部分明确失败）"]
    C_ExplicitFailed -- 否 --> R_SUCCESS["SUCCESS<br>（全部达成，无未决，对账完全一致）"]
    
    C_SuccessCount -- 否 --> C_RetryOnly{存在 RETRYABLE 目标？}
    C_RetryOnly -- 是 --> R_FAILED["FAILED<br>（未成功报名，目标均未达成）"]
    C_RetryOnly -- 否 --> R_FAILED["FAILED<br>（本轮无成功落地，明确失败）"]
```

---

## 四、RETRYABLE 与 UNKNOWN 的语义说明

| 维度 | UNKNOWN / SUBMITTING / VERIFYING (`uncertain_count`) | RETRYABLE (`pending_retry_count`) |
| :--- | :--- | :--- |
| **业务语义** | **结果未知**：请求已发出但因超时或网络 5xx 未收到响应，服务端是否已扣减名额完全不明确。 | **结果明确未报**：已向服务端完整复核，确认服务端目前**没有**该用户的有效报名，用户依然处于未报名状态。 |
| **安全屏障动作** | **立即冻结**：全局互斥阻断，禁止任何新候选提交，必须等待服务端复核求证。 | **允许重新评估**：不阻断其他活动排队；若配额与规则依然允许，可再次评估提交。 |
| **对最终结论的影响** | **只要大于 0，绝对输出 `UNVERIFIED`**：即使其他活动已确认成功落地，也严禁输出 `SUCCESS`。 | **禁止 `SUCCESS`，但不判 `UNVERIFIED`**：若已有其他活动成功落地，判定为 `PARTIAL_SUCCESS`；若落地为 0，判定为 `FAILED`。 |
| **状态转换方向** | 可通过复核/全局对账单向转移至 `SUCCESS` 或 `RETRYABLE`。 | 可通过规则重新校验后进入 `SUBMITTING`，或结束时归档为 `FAILED`。 |

---

## 五、对账状态与 run_id 关联说明

在长生命周期与分布式场景中，若直接读取历史缓存或上一次运行留下的对账报告（例如 `reconciliation_report`），会导致当前运行得出虚假的确定性结论。

Phase 4 实施了严格的会话级隔离：
1. `ReconciliationManager.reconcile()` 自动从绑定的 `audit_logger` 继承当前运行唯一的 `run_id`，并记录在返回字典中：
   ```python
   "run_id": getattr(self.audit_logger, "run_id", None)
   ```
2. `ResultReporter` 接收对账结果时，提取 `self.reconciliation_run_id = reconciliation_report.get("run_id")`。
3. `compute_final_verdict()` 在执行判定时核验：
   ```python
   if self.run_id and getattr(self, "reconciliation_run_id", None):
       if str(self.reconciliation_run_id) != str(self.run_id):
           return (ReportStatus.UNVERIFIED, "对账报告来源 run_id 与当前运行不匹配，判定为未确认！")
   ```
4. 该机制有效防止了跨轮次脏数据污染（经 R21 单元测试严格验证）。

---

## 六、审计降级持久化能力及剩余风险

### 1. 当前已实现的持久化防护机制
- **标记持久化**：当 `AuditLogger` 在写入 JSONL 时遇到 IO 错误、磁盘满或权限异常，除了递增内存中的 `self.write_error_count` 外，会尝试向日志目录持久化写入 `.degraded_{run_id}` 标记文件。
- **回放因果配对分析**：当进程异常退出后，下一轮启动的 `AuditLogReader.replay()` 会：
  1. 扫描是否存在 `.degraded_{run_id}` 标记；
  2. 统计 `unpaired_http_requests = len(started_request_ids - finished_request_ids)`。
- 若任一条件命中，回放汇总字典的 `audit_degraded` 均置为 `True`，使后续生成的报告明确标识降级，杜绝虚假宣称完整。

### 2. 剩余风险说明（Residual Risks）
- **极端断电/内核崩溃时零事件写入**：若进程在写入第一个事件之前发生硬件断电或内核级杀进程（`SIGKILL`），此时磁盘上未留下任何事件记录，也未能写入 `.degraded` 标记文件。在此极极端场景下，离线读取器将视为空日志，无法得知曾发生过异常。
  - *缓解措施*：核心业务数据的最终兜底由 SQLite WAL 模式事务存储保证，状态持久化不受日志文件缺失影响。

---

## 七、R16–R25 补充测试结果

测试文件：[`tests/test_report_consistency.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/tests/test_report_consistency.py)

| 用例编号 | 验证目标 | 结果 |
| :--- | :--- | :--- |
| **R16** | `test_R16_retryable_with_successful_reconcile_not_unverified`：仅存在 RETRYABLE 且对账成功时，输出 `PARTIAL_SUCCESS`，不输出 `UNVERIFIED`。 | ✅ PASS |
| **R17** | `test_R17_unknown_with_successful_registration_must_be_unverified`：存在 UNKNOWN 记录时，即使有成功落地，也强制判定 `UNVERIFIED`。 | ✅ PASS |
| **R18** | `test_R18_retryable_unfulfilled_outputs_partial_or_failed`：存在 RETRYABLE 且目标未达成时，根据已确认报名数准确输出 `PARTIAL_SUCCESS` 或 `FAILED`。 | ✅ PASS |
| **R19** | `test_R19_unperformed_reconciliation_cannot_output_success`：未执行对账时绝对禁止输出 `SUCCESS`，输出 `UNVERIFIED`。 | ✅ PASS |
| **R20** | `test_R20_reconcile_status_none_outputs_unverified`：`reconcile_status=None` 时即使有非零旧值也必须判定为 `UNVERIFIED`。 | ✅ PASS |
| **R21** | `test_R21_previous_run_reconciliation_cannot_pollute_current_run`：跨 `run_id` 的上一轮对账报告被拦截并判定为 `UNVERIFIED`。 | ✅ PASS |
| **R22** | `test_R22_jsonl_write_failure_displays_audit_degraded_in_report`：日志写入失败在报告中准确显示 `audit_degraded: True` 与警告。 | ✅ PASS |
| **R23** | `test_R23_audit_write_failure_crash_prevents_false_integrity_claim`：进程崩溃后重新回放上一轮日志，准确识别降级标记，禁止宣称完整。 | ✅ PASS |
| **R24** | `test_R24_reconciliation_resolves_unknown_to_success`：最终对账识别到服务端已存在该 UNKNOWN 活动，自动更新 StateStore 为 `SUCCESS` 并计入成功。 | ✅ PASS |
| **R25** | `test_R25_reconciliation_incomplete_does_not_clear_unknown`：对账列表残缺 (`complete=False`) 时，严禁解除 UNKNOWN 状态。 | ✅ PASS |

---

## 八、全量测试回归结果

执行全量测试套件：
```bash
python -m tests.run_tests
```
- **测试总数**：359 / 359 项全部通过（100% PASS）
- **测试耗时**：30.088s
- **回归情况**：零回归，历史 334 项与前序 15 项全部稳定通过。

---

## 九、Git Diff 摘要

```diff
 modified:   core/audit_logger.py
 modified:   core/reconciliation.py
 modified:   core/result_reporter.py
 modified:   lejian.py
 modified:   tests/test_report_consistency.py
```
- **状态机与核心规则保护**：
  - Phase 1 熔断器核心逻辑未改动。
  - Phase 2 多进程锁与原子预留未改动。
  - `signUp` 单次提交安全机制未改动。
  - 测试过程未访问任何真实抢课接口。

至此，**Phase 4 结果报告重构与数据一致性正式封版**，具备进入 Phase 5 的完备条件。
