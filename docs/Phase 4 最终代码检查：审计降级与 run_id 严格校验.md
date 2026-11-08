# Phase 4 最终代码检查：审计降级与 run_id 严格校验

当前 Phase 4：

- 全量测试 359/359 PASS
- R01–R25 全部通过
- 已完成正式封版报告

本次只进行两个边界检查。

## 任务一：audit_degraded 必须参与最终结论判定

检查：

- core/result_reporter.py
- core/audit_logger.py
- tests/test_report_consistency.py

确认 compute_final_verdict() 是否同时检查：

1. audit_degraded
2. unpaired_http_requests
3. write_error_count

要求：

只要当前运行的关键审计日志发生写入失败，最终结论必须为 UNVERIFIED。

不能只检查未配对 HTTP 请求。

新增：

R26：audit_degraded=True，但 unpaired_http_requests=0，最终仍然必须 UNVERIFIED。

R27：write_error_count>0，即使最终对账 SUCCESS，也不能输出 SUCCESS。

注意：

- 必须区分当前 run_id 和历史 run_id 的降级标记。
- 不能让历史运行的审计故障污染当前运行。
- 不允许降低现有安全保护。

## 任务二：严格要求 reconciliation_run_id 存在且匹配

检查：

- core/result_reporter.py
- core/reconciliation.py

要求：

只有同时满足以下条件，才允许进入正常最终判定：

1. 当前 reporter.run_id 存在。
2. reconciliation_report.run_id 存在。
3. 两者完全一致。
4. reconciliation_status == SUCCESS。
5. complete == True。

新增：

R28：reconciliation_run_id 缺失，必须 UNVERIFIED。

R29：reporter.run_id 缺失，必须 UNVERIFIED。

R30：对账报告使用历史 run_id，必须 UNVERIFIED。

## 任务三：审查全局对账产生的 Verification 事件

检查 reconciliation.py 和 audit_logger.py。

确认：

- 全局对账解决 UNKNOWN 时，是否产生正确的审计事件。
- 是否把一次全局对账错误统计为多次独立 Verification HTTP 请求。
- Submission、Verification、Reconciliation 三种业务统计是否仍然独立。
- 同一个 UNKNOWN 被对账解决时是否会重复累计配额。

如果事件语义存在混淆，采用最小修改修正。

## 实施要求

1. 先审查现有代码。
2. 输出问题及修改方案。
3. 只修改必要文件。
4. 保留 R01–R25 全部测试。
5. 不修改 Phase 1、Phase 2 核心逻辑。
6. 不访问真实抢课接口。

## 最终交付

- R26–R30 测试结果。
- 全量测试结果。
- 最终判定逻辑。
- Submission / Verification / Reconciliation 统计边界说明。
- Git Diff。
- 尚存风险。

完成后正式冻结 Phase 4，开始 Phase 5。

未经用户授权，不要合并 main。