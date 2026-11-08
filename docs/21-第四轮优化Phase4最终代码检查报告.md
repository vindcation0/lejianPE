# 乐健体育抢课系统 - Phase 4 最终代码检查报告：审计降级与 run_id 严格校验

## 一、概述

本报告记录乐健体育抢课系统第四轮优化 **Phase 4 最终代码检查：审计降级与 run_id 严格校验** 的实施与验收结果。

- **工作分支**：`phase4/result-reporter-refactor`
- **检查目标**：
  1. **强化 `audit_degraded` 判定完整性**：`compute_final_verdict()` 同时强制检查 `audit_degraded`、`write_error_count` 与 `unpaired_http_requests`。当 `audit_degraded=True`（即使未配对数为 0）或 `write_error_count > 0` 时，绝对判定为 `UNVERIFIED`，杜绝任何审计受损时的虚假 `SUCCESS`。
  2. **严格要求对账报告与报告器 `run_id` 存在且一致**：进入正常业务判定流程前，强制要求 `reporter.run_id` 存在、`reconciliation_report.run_id` 存在且两者字符串严格相等。任一缺失或不匹配强制判定为 `UNVERIFIED`。
  3. **审查全局对账产生的 Verification 事件**：严格保证底层物理 HTTP 请求与业务事件的 1:1 准确性，确保 Submission、Verification、Reconciliation 三类业务生命周期完全解耦，配额不产生二次累计。

---

## 二、修改文件清单

| 文件路径 | 修改性质 | 主要改动说明 |
| :--- | :--- | :--- |
| `core/result_reporter.py` | 判定决策树强化 | 1. `compute_final_verdict()` 增加 `reporter.run_id` 与 `reconciliation_run_id` 存在性及强一致性校验（R28、R29、R30）。<br>2. 修复审计降级判定条件，使 `audit_degraded=True`（R26）与 `write_error_count > 0`（R27）均无条件触发 `UNVERIFIED`。 |
| `tests/test_report_consistency.py` | 测试套件补充 | 1. 规范 `test_R03` 与 `test_R13` 对账字典包含合法 `run_id` 与 `status`。<br>2. 新增 R26–R30 共 5 个严密单元测试。 |

---

## 三、最终判定逻辑（Final Verdict Decision Logic）

`ResultReporter.compute_final_verdict()` 遵循以下严格的优先级判断序列：

```mermaid
flowchart TD
    Start([开始最终结论判定]) --> Step1{1. 存在未决记录？<br>uncertain_count > 0<br>(SUBMITTING / UNKNOWN / VERIFYING)}
    Step1 -- 是 --> V_UNVERIFIED["UNVERIFIED<br>（存在未决状态，安全屏障生效）"]
    
    Step1 -- 否 --> Step2_1{2.1 是否执行过全局对账？<br>reconciliation_performed == True}
    Step2_1 -- 否 --> V_UNVERIFIED
    
    Step2_1 -- 是 --> Step2_2{2.2 对账是否明确通过且完整？<br>reconciliation_status == 'SUCCESS'<br>AND complete == True}
    Step2_2 -- 否 --> V_UNVERIFIED
    
    Step2_2 -- 是 --> Step2_3{2.3 reporter.run_id 是否存在？<br>bool(reporter.run_id) == True}
    Step2_3 -- 否 --> V_UNVERIFIED["UNVERIFIED<br>（当前报告缺少 run_id，归属无法确认）"]
    
    Step2_3 -- 是 --> Step2_4{2.4 reconciliation_report.run_id 是否存在？<br>bool(reconcile_rid) == True}
    Step2_4 -- 否 --> V_UNVERIFIED["UNVERIFIED<br>（对账报告缺少 run_id，时效无法确认）"]
    
    Step2_4 -- 是 --> Step2_5{2.5 run_id 是否完全匹配？<br>reconcile_rid == reporter.run_id}
    Step2_5 -- 否 --> V_UNVERIFIED["UNVERIFIED<br>（跨 run_id 污染拦截）"]
    
    Step2_5 -- 是 --> Step3{3. 审计是否发生降级或写入失败？<br>audit_degraded OR<br>write_error_count > 0 OR<br>unpaired_http_requests > 0}
    Step3 -- 是 --> V_UNVERIFIED["UNVERIFIED<br>（审计数据不完整或日志写入受损）"]
    
    Step3 -- 否 --> Step4{4. 服务端确认报名数量<br>reconciled_success > 0 ?}
    Step4 -- 是 --> CheckLimits{是否存在超额？}
    CheckLimits -- 是 --> V_PARTIAL["PARTIAL_SUCCESS<br>（超出每日/每周配额）"]
    CheckLimits -- 否 --> CheckRetry{存在 RETRYABLE 待重试？}
    CheckRetry -- 是 --> V_PARTIAL["PARTIAL_SUCCESS<br>（存在未达成重试目标）"]
    CheckRetry -- 否 --> CheckExplicitFail{存在明确失败？}
    CheckExplicitFail -- 是 --> V_PARTIAL["PARTIAL_SUCCESS<br>（部分成功，部分明确失败）"]
    CheckExplicitFail -- 否 --> V_SUCCESS["SUCCESS<br>（全部达成，对账一致，审计完整）"]
    
    Step4 -- 否 --> CheckRetryOnly{存在 RETRYABLE 目标？}
    CheckRetryOnly -- 是 --> V_FAILED["FAILED<br>（未成功报名，重试目标未达成）"]
    CheckRetryOnly -- 否 --> V_FAILED["FAILED<br>（本轮无成功落地，明确失败）"]
```

---

## 四、Submission / Verification / Reconciliation 统计边界说明

为了杜绝指标混淆与重复累计，第四轮优化对三类业务操作建立了物理与语义上的严格边界：

| 统计类别 | 触发时机 | 对应的 Schema v1 事件 | 包含的底层 HTTP 请求 | 产生的统计指标 |
| :--- | :--- | :--- | :--- | :--- |
| **真实报名提交 (Submission)** | 仅当系统向服务端 `signUp` 接口真实发起网络写入时触发。 | `submission_started`, `submission_response_received`, `submission_succeeded`, `submission_failed`, `submission_unknown` | 每次调用产生 1 次专属 `request_id` 的 HTTP 请求。 | `attempts.total`, `explicit_success`, `explicit_failed`, `unknown` |
| **UNKNOWN 服务端复核 (Verification)** | 仅在提交遭遇 5xx/超时进入 UNKNOWN 后，或启动恢复时对未决活动求证时触发。 | `verification_started`, `verification_request_succeeded`, `verification_request_failed`, `verification_confirmed_registered`, `verification_confirmed_unregistered`, `verification_remains_unknown` | 仅在调用独立复核接口 (`getActivityList`) 时产生 HTTP 请求。若由全局对账顺带解除，则复用对账请求，**不产生冗余 HTTP 请求**。 | `verification.total`, `confirmed_registered`, `confirmed_unregistered`, `remains_unknown` |
| **全局服务端对账 (Reconciliation)** | 抢课循环结束或满额退出时，全局核对用户全量已报课程。 | `reconciliation_started`, `reconciliation_succeeded`, `reconciliation_failed`, `reconciliation_incomplete` | 仅发起 1 次 `activities(page_size=200)` 请求。 | `reconciliation.confirmed_success`, `confirmed_failed`, `total_server_registered`, `by_week`, `by_day` |

### 核心保证：
1. **零冗余 HTTP 统计**：全局对账解决 N 个 UNKNOWN 时，底层仅发生了 1 次全量列表拉取请求，绝不将 1 次对账错误虚增为 N 次独立 Verification 失败。
2. **零配额重复累计**：全局对账以服务端返回的 `registered_items` 为唯一基准（去重），即便同一活动经历过提交、复核和对账，配额只按服务端实际列表计算一次。

---

## 五、R26–R30 专项补充测试结果

测试文件：[`tests/test_report_consistency.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/tests/test_report_consistency.py)

| 测试编号 | 测试点与断言要求 | 结果 |
| :--- | :--- | :--- |
| **R26** | `test_R26_audit_degraded_without_unpaired_requests_must_be_unverified`：`audit_degraded=True` 且 `unpaired_http_requests=0`，即使对账成功也必须判定为 `UNVERIFIED`。 | ✅ PASS |
| **R27** | `test_R27_write_error_count_prevents_success_verdict`：`write_error_count > 0` 发生写失败，即使最终对账成功也必须判定为 `UNVERIFIED`。 | ✅ PASS |
| **R28** | `test_R28_missing_reconciliation_run_id_outputs_unverified`：对账报告缺失 `run_id`，无法确认对账时效性与归属，输出 `UNVERIFIED`。 | ✅ PASS |
| **R29** | `test_R29_missing_reporter_run_id_outputs_unverified`：当前报告缺少 `run_id`，输出 `UNVERIFIED`。 | ✅ PASS |
| **R30** | `test_R30_stale_historical_reconciliation_run_id_outputs_unverified`：对账报告来源 `run_id` 与当前报告不匹配（跨轮次污染），输出 `UNVERIFIED`。 | ✅ PASS |

---

## 六、全量测试回归结果

执行全量测试套件：
```bash
python -m tests.run_tests
```
- **测试总数**：364 / 364 全部通过（100% PASS）
- **测试耗时**：29.57s
- **回归情况**：零回归。所有历史测试（包括 334 个前序测试与 R01–R30 全部 30 个一致性测试）全部稳定通过。

---

## 七、Git Diff 摘要

```diff
 modified:   core/result_reporter.py
 modified:   tests/test_report_consistency.py
```
- 严格化了 `compute_final_verdict()` 中的 `run_id` 匹配条件与审计完整性判定。
- Phase 1 熔断器、Phase 2 多进程锁与原子预留未做任何变动。

---

## 八、尚存风险

1. **极端操作系统掉电风险**：
   - 若进程在发起任何文件系统写操作前被物理断电或强行 `kill -9`，日志文件中可能未留下任何痕迹。该风险通过本地 SQLite WAL 模式数据库事务作为业务数据的终极保障。
2. **多机跨节点分布式场景**：
   - 当前 `run_id` 与状态库针对单机单用户环境设计。若后续扩展为跨多台服务器分布式抢课，需在 Phase 5 考虑引入中心化调度。

---

## 九、结论

Phase 4 的结果报告重构与数据一致性验证已达成所有指标，所有边界条件与防御机制均已严格就位，**正式冻结 Phase 4，进入 Phase 5**。
