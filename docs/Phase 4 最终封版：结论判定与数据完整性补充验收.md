# Phase 4 最终封版：结论判定与数据完整性补充验收

当前 Phase 4 已完成主体重构。

当前测试基线：

- Phase 3：334 项通过
- Phase 4：349 项通过
- R01–R15 全部通过

本次只做最后的边界检查，不进行大规模重构。

## 一、任务一：修正 RETRYABLE 与 UNKNOWN 的语义边界

检查：

- core/result_reporter.py
- core/state_store.py
- core/reconciliation.py

目前报告将 SUBMITTING、UNKNOWN、VERIFYING、RETRYABLE 全部纳入 unresolved_count。

请核实是否合理。

要求区分：

uncertain_count：

- SUBMITTING
- UNKNOWN
- VERIFYING

pending_retry_count：

- RETRYABLE

判定原则：

1. uncertain_count > 0，必须 UNVERIFIED。
2. pending_retry_count > 0，禁止 SUCCESS，但不应仅因为 RETRYABLE 就判定 UNVERIFIED。
3. 已明确确认未报名，但没有完成后续目标，应根据实际完成情况判定 PARTIAL_SUCCESS 或 FAILED。
4. 不得因为拆分状态而放宽 UNKNOWN 安全屏障。
5. 保留现有 StateStore 状态转换规则。

新增测试：

R16：仅存在 RETRYABLE，且最终对账成功，不能输出 UNVERIFIED。

R17：存在 UNKNOWN，即使已有成功报名，也必须 UNVERIFIED。

R18：存在 RETRYABLE 且目标未达成，正确输出 PARTIAL_SUCCESS 或 FAILED。

## 二、任务二：最终对账必须明确为 SUCCESS

检查 compute_final_verdict()。

要求：

只有 reconcile_status == SUCCESS 才能进入正常成功判定流程。

以下情况必须输出 UNVERIFIED：

- reconcile_status 为 None。
- reconcile_status 缺失。
- 对账尚未执行。
- 对账 FAILED。
- 对账 UNVERIFIED。
- 对账 complete=False。

不能只通过排除 FAILED 和 UNVERIFIED 来判断成功。

新增测试：

R19：没有执行最终对账，不能输出 SUCCESS。

R20：reconcile_status=None，即使 confirmed_total 存在非零旧值，也必须 UNVERIFIED。

R21：使用上一次运行的 reconciliation_report，不能污染当前 run_id 的最终结论。

## 三、任务三：审计日志故障恢复检查

检查 audit_degraded 和 write_error_count。

新增测试：

R22：JSONL 写入失败后报告必须显示 audit_degraded。

R23：审计写入失败后进程异常退出，重新启动后不能把上一轮日志宣称为完整。

如果当前机制无法持久化上一轮审计降级状态，请明确记录为剩余风险，不要虚构已经解决。

## 四、任务四：全局对账解除 UNKNOWN 的状态一致性

检查最终对账成功后：

- 是否识别服务端已经报名的 UNKNOWN 活动。
- 是否正确更新 StateStore。
- 是否正确更新配额。
- 是否留下完整审计事件。
- 是否避免重复累计。

新增测试：

R24：最终对账发现 UNKNOWN 活动已报名，正确更新 SUCCESS。

R25：最终对账确认活动未报名时，不能在数据不完整的情况下解除 UNKNOWN。

## 五、实施要求

1. 先检查代码，不直接修改。
2. 输出问题和最小修改方案。
3. 仅修改必要文件。
4. 不修改 Phase 1 熔断器核心逻辑。
5. 不修改 Phase 2 多进程锁和原子预留。
6. 不改变 signUp 单次提交安全机制。
7. 保留所有历史测试。
8. 不访问真实抢课接口。

## 六、最终交付

提供：

1. R16–R25 测试结果。
2. 全量测试结果。
3. 最终结论判定树。
4. RETRYABLE 与 UNKNOWN 的语义说明。
5. 对账状态与 run_id 关联说明。
6. 审计降级持久化能力及剩余风险。
7. Git Diff。

完成后正式封版 Phase 4，进入 Phase 5。

未经用户授权，不要合并 main。