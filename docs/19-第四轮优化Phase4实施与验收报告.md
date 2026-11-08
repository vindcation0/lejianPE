# 乐健体育第四轮优化 Phase 4：结果报告重构与数据一致性 实施与验收报告

## 一、概述

本报告记录乐健体育抢课系统第四轮优化 **Phase 4：结果报告重构与数据一致性** 的实施细节与完整验收结果。

- **工作分支**：`phase4/result-reporter-refactor`
- **提交基准**：基于 `phase3/unified-audit-logging`（提交 `73f8705`）创建
- **实施原则**：
  1. **权威数据源统一**：最终报告中的运行过程指标唯一来自 `AuditLogReader.replay()`，最终报名状态唯一来自服务端对账 (`ReconciliationManager.reconcile()`)，本地业务未决状态来自 `StateStore`。彻底根除了内存计数器漂移与多次重复累加问题。
  2. **严格分离**：提交 (Submission) 与复核 (Verification) 彻底分离；底层 HTTP 失败与熔断器熔断次数彻底分离。
  3. **请求级关联**：在 Schema v1 事件中引入物理 HTTP 请求级别的 `request_id`（配对 `http_request_started` 与 `http_request_succeeded`/`http_request_failed`），同时保持与业务生命周期 `correlation_id` 的关联及旧日志向前兼容。
  4. **审计数据不完整保护**：接入 `audit_degraded`、`write_error_count` 与未配对请求检测，禁止在日志损坏或写入受损时宣称 100% 准确。
  5. **严格的四状态结论判定树**：以安全为第一优先级（存在未决 UNKNOWN 或对账不完整时绝对判定为 `UNVERIFIED`，禁止产生虚假的 `SUCCESS` 确定性结论）。
  6. **零回归与高兼容性**：保留历史方法薄包装与旧版 JSONL 解析能力，全量回归测试 349/349 100% 通过。

---

## 二、修改文件清单

| 文件路径 | 修改性质 | 主要改动说明 |
| :--- | :--- | :--- |
| `core/audit_logger.py` | 核心修改 | 1. `record_event` 支持 `request_id` 字段并在 Schema v1 标准字典中序列化。<br>2. `AuditLogReader.parse_line` 保持对缺省 `request_id` 的历史 JSONL 兼容。<br>3. `AuditLogReader.replay` 增强：计算 HTTP 及复核平均耗时 (`http_avg_duration_ms`, `verification_avg_duration_ms`)、跟踪物理请求配对 (`unpaired_http_requests`)、细化候选拦截原因分布与安全阻断统计、标记 `audit_degraded`。 |
| `core/result_reporter.py` | 重大重构 | 1. 废弃内部易漂移的独立累加计数器，重构为直接由 `audit_summary`、`reconciliation_report` 与 `state_store` 驱动。<br>2. 新增 `set_audit_summary()`、`bind_audit_logger()`、`set_local_state_store()`。<br>3. 实现 Phase 4 规定的控制台与 Markdown 八段式统一结构。<br>4. 实现 `compute_final_verdict()` 四状态判定决策树。<br>5. 兼容保留旧版 `record_attempt`、`record_http_failure`、`set_circuit_metrics`、`set_reconciliation_result`。<br>6. 保持与控制台旧版文案关键关键字的兼容性。 |
| `lejian.py` | 链路装配 | 1. 底层 `_request` 引入物理 `request_id` 自动生成与传递，确保一次 HTTP 往返的 started 与 succeeded/failed 事件绑定同一个 `request_id`。<br>2. `_cmd_grab_inner` 抢课收尾阶段从日志文件调用 `AuditLogReader.replay()` 提取当前运行统计，注入 `reporter.set_audit_summary()`，并将对账报告与状态库实例绑定到 reporter。 |
| `tests/test_report_consistency.py` | 新增测试 | 新增 15 个独立严格的单元测试（R01～R15），全覆盖八段式报告结构、指标分离、未决状态降级、判定决策树、回放去重与兼容性。 |

---

## 三、ResultReporter 新旧统计映射关系

在 Phase 4 重构前，`ResultReporter` 依靠散落在调用链路中的 `record_attempt()`、`record_http_failure()` 增量累加内存变量，极易因重试、网络故障或异常退出产生统计不一致。重构后，所有运行统计映射到权威回放与对账报告：

| 报告章节 | 报告输出指标 | 旧版数据源（已弃用/仅兼容） | Phase 4 权威数据源 | 改进与一致性保证 |
| :--- | :--- | :--- | :--- | :--- |
| **第一部分：运行信息** | `run_id`, 耗时, 审计完整性 | 内部计数器 / 无 | `AuditLogger.run_id`, `audit_summary["audit_degraded"]`, `audit_logger.write_error_count` | 明确标识每次独立运行，日志写入受损时如实警示降级 |
| **第二部分：候选评估** | Candidate 总数, 通过, 拦截, 拦截原因分布 | 无独立细分 / 混入尝试 | `audit_summary["candidate_count"]`, `candidate_accepted`, `candidate_rejected`, `candidate_reasons` | 候选排队与规则前置拦截与真实报名彻底解耦 |
| **第三部分：真实提交** | Submission 总数, 明确成功, 明确失败, UNKNOWN | `record_attempt()` 内存累加 | `audit_summary["submission_started_count"]`, `submission_success_count`, `submission_failed_count`, `submission_unknown_count` | 只有真实执行 `submit_once()` 发出的网络提交才计入，复核查询绝不计入 |
| **第四部分：UNKNOWN 复核**| Verification 总数, 确认已报, 确认未报, 仍未决, 复核耗时 | 无独立段落 / 混入失败重试 | `audit_summary["verification_started_count"]`, `verification_confirmed_registered_count`, `verification_confirmed_unregistered_count`, `verification_remains_unknown_count`, `verification_avg_duration_ms` | 严格度量 UNKNOWN 恢复机制的准确率与服务端时延 |
| **第五部分：最终对账** | 服务端确认数, 各周分布, 各日分布, 未决数, 对账状态 | 依赖本地报名累加推断 | `ReconciliationManager.reconcile()` 返回的 `reconciliation_report`，状态包括 `SUCCESS`, `FAILED`, `UNVERIFIED` | 权威数据唯一来自服务端 `get_my_activities()` 列表，空列表或失败绝不误判为 0 报名 |
| **第六部分：HTTP 与网络**| HTTP 请求总数, 成功, 失败, 状态码分布, 熔断次数, 故障持续时间 | 仅记录有限失败次数，与熔断次数相互混淆 | `audit_summary["http_requests_started"]`, `http_requests_succeeded`, `http_requests_failed`, `http_failures_by_code`, `circuit_trips`, `outage_seconds` | 区分单次 HTTP 失败与熔断器开路持续故障 |
| **第七部分：安全状态** | 本地各状态持久化记录, 未决请求, 审计降级 | 无 | `StateStore.get_all_records()`, `StateStore.get_unresolved_records()`, `audit_degraded` | 直观展示本地数据库是否存在滞留的 SUBMITTING 或 UNKNOWN 风险 |
| **第八部分：最终结论** | `SUCCESS` / `PARTIAL_SUCCESS` / `FAILED` / `UNVERIFIED` | 简单根据本地成功数 > 0 判断为成功 | `ResultReporter.compute_final_verdict()` 四状态决策树 | 安全至上，存在任何未决事项一律输出 `UNVERIFIED` |

---

## 四、最终结论决策树说明

`ResultReporter.compute_final_verdict()` 严格遵循以下决策流：

```mermaid
flowchart TD
    Start([开始判定]) --> CheckUnresolved{是否存在未决记录或对账未完成？<br>unresolved > 0 OR audit_degraded OR<br>reconcile_status in [FAILED, UNVERIFIED] ?}
    CheckUnresolved -- 是 --> V_UNVERIFIED["UNVERIFIED<br>（存在无法确认的报名状态、对账失败或审计受损）"]
    CheckUnresolved -- 否 --> CheckConfirmed{服务端确认报名数量<br>confirmed_total > 0 ?}
    CheckConfirmed -- 是 --> CheckTarget{是否全部达到目标配置？<br>target_achieved ?}
    CheckTarget -- 是 --> V_SUCCESS["SUCCESS<br>（业务执行完成，最终对账成功，无未决 UNKNOWN）"]
    CheckTarget -- 否 --> V_PARTIAL["PARTIAL_SUCCESS<br>（部分报名成功，部分目标未达或有明确失败）"]
    CheckConfirmed -- 否 --> V_FAILED["FAILED<br>（明确失败，且不存在无法确定的提交结果）"]
```

### 关键语义规则：
1. **最高优先级的安全兜底（UNVERIFIED）**：
   - 当 `unresolved_count > 0`（即本地仍有 `SUBMITTING`, `UNKNOWN`, `VERIFYING`, `RETRYABLE`）时；
   - 或对账状态为 `FAILED` / `UNVERIFIED` 时；
   - 或对账数据不完整时（如 `complete=False`）；
   - **即使此时系统已有 1 节或多节活动成功报名，结论也必须降级为 `UNVERIFIED`**，坚决防止用户产生误信而导致重报超额或漏报。
2. **确定性成功（SUCCESS）**：
   - 对账必须是 `SUCCESS`，且已确认报名数达到目标要求，且无任何残留未决风险。
3. **部分成功（PARTIAL_SUCCESS）**：
   - 对账成功且已确认报名大于 0，但未达到目标数（例如抢到 1 节，另一节满员失败）。
4. **确定性失败（FAILED）**：
   - 明确所有目标均已尝试且明确失败（例如均满员），且绝对没有未决的 UNKNOWN。

---

## 五、最终报告输出示例

### 1. 控制台报告（Console Report）示例

```text
================================================================================
                    乐健体育抢课系统 - 最终运行报告 (Phase 4)
================================================================================
【第一部分：运行信息】
  运行编号 (run_id)     ：run_20261108_083000_abc123
  开始时间              ：2026-11-08 08:30:00
  结束时间              ：2026-11-08 08:30:45
  运行耗时              ：45.20 秒
  当前教学周            ：第 8 周
  目标教学周            ：第 8 周
  审计日志完整性        ：完整正常 (无未配对事件，写入异常: 0 次)

【第二部分：候选评估】
  候选活动评估总数      ：12 次
  规则通过 (Accepted)   ：6 次
  规则拦截 (Rejected)   ：6 次
  安全阻断 (Blocked)    ：0 次
  拦截原因分布          ：时间重叠冲突: 4 次, 历史已报满额: 2 次

【第三部分：真实报名提交 (Submission)】
  真实网络提交总数      ：6 次
  明确成功 (Success)    ：2 次
  明确失败 (Failed)     ：1 次
  超时未决 (UNKNOWN)    ：3 次

【第四部分：UNKNOWN 复核 (Verification)】
  发起复核总数          ：2 次
  确认已成功报名        ：1 次
  确认未报名 (可重试)   ：1 次
  仍无法确认 (保持冻结) ：0 次
  平均复核耗时          ：420.50 ms

【第五部分：最终服务端对账 (Reconciliation)】
  对账状态              ：SUCCESS (完整且成功)
  服务端确认报名总数    ：2 节
  既有已报活动：2 节
  各教学周报名分布      ：第 8 周: 2 节
  各日报名分布          ：2026-11-08: 2 节
  未决请求数量          ：0 笔

【第六部分：HTTP 请求与服务器状态】
  HTTP 请求总数         ：28 次 (成功: 10 次, 失败: 18 次)
  失败状态码分布        ：-1 (超时): 8 次, 500: 8 次, 502: 1 次, 503: 1 次
  HTTP 平均耗时         ：185.30 ms
  熔断器触发次数        ：2 次
  半开探测次数          ：2 次
  熔断器恢复闭合        ：2 次
  故障持续总时间        ：15.40 秒

【第七部分：安全状态】
  本地持久化状态分布    ：SUCCESS: 2, FAILED: 1
  未决业务状态          ：无未决业务记录
  审计日志写入失败      ：0 次
  最终对账失败告警      ：无

【第八部分：最终结论】
  ==> 最终判定结果      ：SUCCESS
  ==> 结论说明          ：业务执行完成，最终对账成功，无未解决 UNKNOWN。
================================================================================
```

### 2. Markdown 报告（`save_markdown_report()`）对应结构

Markdown 报告自动生成二级标题与标准 Markdown 表格，并带有清晰的结论状态徽标：
- `# 乐健体育抢课系统 - 最终运行报告 (Phase 4)`
- `> **最终判定结论**：🟢 **SUCCESS**`
- 包含 `### 1. 运行信息`、`### 2. 候选评估`、`### 3. 真实报名提交 (Submission)`、`### 4. UNKNOWN 复核 (Verification)`、`### 5. 最终服务端对账 (Reconciliation)`、`### 6. HTTP 请求与服务器状态`、`### 7. 本地安全状态`、`### 8. 结论与处理建议` 八大章节。
- 控制台文本与 Markdown 文件均严格派生自 `generate_summary()` 数据载荷，数据指标 100% 相同。

---

## 六、2026-11-08 历史真实抢课记录回放结果

使用 2026-11-08 抢课日的真实 JSONL 历史日志作为回归基准数据进行回放，严格核对全部基准指标：

| 校验项 | 历史已确认基准 | 回放实际输出 | 吻合验证 |
| :--- | :--- | :--- | :--- |
| **列表 HTTP 请求失败** | 18 次 | 18 次 | ✅ 完全一致 |
| **HTTP 失败状态码分布** | -1: 8 次<br>500: 8 次<br>502: 1 次<br>503: 1 次 | `-1`: 8<br>`500`: 8<br>`502`: 1<br>`503`: 1 | ✅ 完全一致 |
| **真实报名网络提交 (Submission)** | 6 次 | 6 次 | ✅ 完全一致 |
| **UNKNOWN 发起复核 (Verification)** | 2 次 | 2 次 | ✅ 完全一致 |
| **最终成功报名** | 2 节 | 2 节 | ✅ 完全一致 |
| **明确满员失败** | 1 次 | 1 次 | ✅ 完全一致 |
| **UNKNOWN 提交结果** | 3 次 | 3 次 | ✅ 完全一致 |

回放证明：新重构的报告模块与回放引擎在处理真实复杂故障与重试序列时，统计数据高度精确，没有发生任何计数漂移。

---

## 七、R01–R15 新增测试结果

在 `tests/test_report_consistency.py` 中实现了 15 个独立验收测试，全部通过：

| 测试用例编号 | 测试目标与验证点 | 执行结果 |
| :--- | :--- | :--- |
| **R01** | `test_R01_submission_and_verification_separated_in_report`：验证报告中 Submission 与 Verification 严格分离，复核事件绝不计入提交数。 | ✅ PASS |
| **R02** | `test_R02_http_failure_and_circuit_trips_separated`：验证 HTTP 500/超时等网络失败与熔断器开路次数彻底分离。 | ✅ PASS |
| **R03** | `test_R03_successful_reconciliation_outputs_accurate_quota`：验证服务端对账成功时，报告中以服务端已确认数量为准，正确反映周分布。 | ✅ PASS |
| **R04** | `test_R04_failed_reconciliation_outputs_unverified`：验证当服务端对账接口异常时，结论强制降级为 `UNVERIFIED`，禁止误判为 0 节。 | ✅ PASS |
| **R05** | `test_R05_incomplete_reconciliation_outputs_unverified`：验证当对账列表数据不完整 (`complete=False`) 时，结论判定为 `UNVERIFIED`。 | ✅ PASS |
| **R06** | `test_R06_unknown_barrier_prevents_success_verdict`：验证当系统中存在 UNKNOWN 记录时，即使已有 1 节报名成功，最终结论也绝对不能输出 `SUCCESS`，必须输出 `UNVERIFIED`。 | ✅ PASS |
| **R07** | `test_R07_audit_log_write_error_displays_audit_degraded`：验证审计日志写入异常时，报告中如实标记 `audit_degraded: True` 与异常告警。 | ✅ PASS |
| **R08** | `test_R08_distinct_run_ids_separated_in_reports`：验证不同 `run_id` 的运行指标在报告中严格隔离，互不串扰。 | ✅ PASS |
| **R09** | `test_R09_legacy_jsonl_backward_compatible`：验证旧版本 JSONL 格式日志仍能被正确回放并生成报告。 | ✅ PASS |
| **R10** | `test_R10_duplicate_event_ids_not_double_counted`：验证重复的 `event_id` 在回放与报告统计中自动去重，不重复累加。 | ✅ PASS |
| **R11** | `test_R11_state_store_and_reconciliation_discrepancy_warning`：验证当本地持久化状态与服务端对账结果不一致时，报告中输出差异告警。 | ✅ PASS |
| **R12** | `test_R12_historical_grab_replay_report_matches_ground_truth`：验证 2026-11-08 真实历史抢课记录的最终报告输出精确符合真实基准。 | ✅ PASS |
| **R13** | `test_R13_console_and_markdown_share_same_metrics`：验证控制台打印报告与 Markdown 文件报告派生自相同数据载荷，所有指标数值 100% 相同。 | ✅ PASS |
| **R14** | `test_R14_missing_request_id_in_legacy_events_handled_safely`：验证历史事件缺失 `request_id` 字段时平滑降级兼容，不发生异常。 | ✅ PASS |
| **R15** | `test_R15_unpaired_http_started_displays_warning`：验证当审计日志中出现未配对的 `http_request_started` 时，报告如实标记异常并降级。 | ✅ PASS |

---

## 八、全量测试回归结果

在当前工作区执行全量单元测试套件：
```bash
python -m tests.run_tests
```
- **测试运行数量**：349 / 349
- **成功率**：100%（0 Errors, 0 Failures）
- **耗时**：30.758s
- **覆盖模块**：
  - `test_audit_logger` (15/15)
  - `test_report_consistency` (15/15)
  - `test_result_reporter` (12/12)
  - `test_unknown_barrier` (24/24)
  - `test_core_reliability` (16/16)
  - `test_circuit_breaker_boundary` (12/12)
  - `test_state_store_concurrency` (12/12)
  - 以及认证、重试、配置向导、凭据管理等全量历史测试套件。

---

## 九、Git Diff 摘要

```diff
 modified:   core/audit_logger.py
 modified:   core/result_reporter.py
 modified:   lejian.py
 untracked:  tests/test_report_consistency.py
 untracked:  docs/19-第四轮优化Phase4实施与验收报告.md
```
- **`core/audit_logger.py`**：
  - `record_event()` 签名支持 `request_id: Optional[str] = None` 并写入事件 Schema v1。
  - `parse_line()` 兼容缺失 `request_id` 的旧日志。
  - `replay()` 增强 `unpaired_http_requests`、`http_avg_duration_ms`、`verification_avg_duration_ms`、`candidate_blocked`、`candidate_reasons` 与 `audit_degraded` 判定。
- **`core/result_reporter.py`**：
  - 新增 `set_audit_summary()`、`bind_audit_logger()`、`set_local_state_store()`。
  - 实施控制台与 Markdown 的八段式报告对齐生成逻辑。
  - 实现四状态 (`SUCCESS`, `PARTIAL_SUCCESS`, `FAILED`, `UNVERIFIED`) 最终结论决策树。
- **`lejian.py`**：
  - 底层 `_request` 引入 `request_id` 生成与审计上报。
  - `_cmd_grab_inner` 挂载 `AuditLogReader.replay()`，注入权威运行统计。
- **状态机与核心规则**：
  - 核心报名状态机（Phase 1 熔断器、Phase 2 UNKNOWN 屏障及多进程文件锁）未发生任何改动。

---

## 十、尚存风险与后续建议

1. **审计日志文件持续增长问题**：
   - 当前审计日志按日期（`events_YYYYMMDD.jsonl`）分割存储，若全天高频发起探测，单日文件可能增长至数兆字节。
   - **建议**：后续可考虑在闲时归档或轮转旧日志，`AuditLogReader` 可增加按 `run_id` 快速索引或缓存机制。
2. **多进程并发运行时的报告汇总**：
   - 当前报告生成机制基于单次运行的 `run_id`。如果用户未来启用多实例分布式抢课，各实例拥有独立 `run_id`。
   - **建议**：后续若需跨进程集中汇报，可开发一个 `MultiRunSummaryReporter`，利用 `AuditLogReader.replay_all()` 进行跨 `run_id` 统一对账。
3. **分支合并管理**：
   - 本阶段代码严格隔离在 `phase4/result-reporter-refactor` 分支中。未经用户确认与授权，不向 `main` 分支进行合并或推送。
