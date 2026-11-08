# 乐健体育第四轮优化 Phase 4：结果报告重构与数据一致性

## 一、当前项目进度

Phase 0：问题确认完成。

Phase 1：熔断器修复完成。

Phase 2：UNKNOWN 安全屏障及多进程保护正式封版。

Phase 3：统一审计日志与事件统计正式完成。

当前测试基线：

334/334 PASS。

当前已具备：

- AuditLogger Schema Version 1
- AuditLogReader.replay()
- 五类统一事件
- HTTP 底层请求统计
- Submission 与 Verification 分离
- 历史真实抢课记录回放
- 事件幂等去重
- 敏感信息脱敏

现在进入 Phase 4。

## 二、本阶段核心目标

重构 ResultReporter，使最终报告的数据直接来自统一事件审计体系。

解决以下问题：

1. 内存计数器与实际事件不一致。
2. 报告中存在重复统计。
3. 提交、复核、对账统计口径不一致。
4. HTTP 请求次数和故障次数混淆。
5. 最终报告缺少 UNKNOWN 风险提示。
6. 对账失败时可能产生错误的确定性结论。
7. 历史报告和当前报告结构不统一。

本阶段不修改已经封版的核心报名状态机。

## 三、重点检查文件

- core/result_reporter.py
- core/audit_logger.py
- core/reconciliation.py
- core/request_manager.py
- core/registration_manager.py
- lejian.py
- tests/test_result_reporter.py
- tests/test_audit_logger.py

首先审查现有 ResultReporter，确认哪些内存计数器已经可以被事件回放替代。

不要直接删除所有旧计数器。

必须先建立指标映射，再逐项迁移。

## 四、任务一：统一报告数据来源

建议明确三种数据来源。

### 1. 运行过程指标

唯一来源：

AuditLogReader.replay()

包括：

- 候选评估次数
- 候选通过次数
- 候选拦截次数
- 真实提交次数
- UNKNOWN 次数
- 复核次数
- HTTP 请求次数
- HTTP 失败次数
- 熔断次数
- 故障持续时间

### 2. 最终报名状态

唯一可信来源：

完整且成功的服务端 Reconciliation。

包括：

- 服务端确认报名数量
- 各教学周报名分布
- 每日报名分布
- 普通活动数量
- 特殊活动数量
- 是否达到每日或每周上限

如果最终对账失败或不完整：

必须标记 UNVERIFIED。

禁止将空列表解释为零报名。

### 3. 本地持久化状态

来源：

StateStore。

用于展示：

- 当前 SUBMITTING
- 当前 UNKNOWN
- 当前 VERIFYING
- 当前 RETRYABLE
- 尚未解决的业务状态

本地状态不能覆盖服务端最终对账结果。

## 五、任务二：重构 ResultReporter

建议让 ResultReporter 接收 AuditLogReader 的回放结果。

避免再次维护一套独立的统计计数器。

如果历史接口需要兼容，可以保留薄封装，但必须确保所有最终统计来自统一数据源。

报告至少分为以下部分：

### 第一部分：运行信息

- run_id
- 开始时间
- 结束时间
- 运行持续时间
- 当前教学周
- 目标教学周
- 审计日志完整性

### 第二部分：候选评估

- Candidate 总数
- Accepted 数量
- Rejected 数量
- Blocked 数量
- 各类拦截原因

### 第三部分：真实报名提交

- Submission 总数
- 明确成功数量
- 明确失败数量
- UNKNOWN 数量

必须保证 Verification 不进入 Submission。

### 第四部分：UNKNOWN 复核

- Verification 总数
- 确认已报名数量
- 确认未报名数量
- 仍然 UNKNOWN 数量
- 平均复核耗时

### 第五部分：最终服务端对账

- 对账状态
- 服务端确认报名数量
- 各周报名分布
- 各日报名分布
- 未决请求数量

对账状态必须区分：

SUCCESS
FAILED
UNVERIFIED

### 第六部分：HTTP 请求与服务器状态

- HTTP 请求总数
- HTTP 成功次数
- HTTP 失败次数
- 状态码分布
- 超时次数
- 网络异常次数
- 平均请求耗时
- 熔断触发次数
- 半开探测次数
- 恢复次数
- 故障持续时间

HTTP 失败次数与熔断次数必须分开。

### 第七部分：安全状态

- SUBMITTING 数量
- UNKNOWN 数量
- VERIFYING 数量
- RETRYABLE 数量
- 是否存在未决请求
- 是否存在审计日志写入失败
- 是否存在最终对账失败

### 第八部分：最终结论

明确输出：

SUCCESS
PARTIAL_SUCCESS
FAILED
UNVERIFIED

建议语义：

SUCCESS：
业务执行完成，最终对账成功，无未解决 UNKNOWN。

PARTIAL_SUCCESS：
有部分报名成功，但部分目标未完成或存在明确业务失败，且最终状态已验证。

FAILED：
明确失败，且不存在无法确定的提交结果。

UNVERIFIED：
存在无法确认的报名状态、最终对账失败或关键审计数据不完整。

如果同时存在成功报名与 UNKNOWN，必须优先突出 UNVERIFIED 风险，不能仅凭已有成功记录输出 SUCCESS。

## 六、任务三：增加请求级关联能力

检查 Phase 3 的事件 Schema。

建议增加 request_id：

- 每次底层 HTTP 请求生成独立 request_id。
- started 与 succeeded/failed 使用同一个 request_id。
- 不同 HTTP 重试使用不同 request_id。
- 同一业务 correlation_id 可以关联多个 request_id。

如果发现历史事件缺少 request_id，必须保持向后兼容。

不要为了增加字段破坏旧 JSONL 读取能力。

## 七、任务四：审计数据不完整保护

处理 A10 中 JSONL 写入失败的情况。

要求：

1. 记录审计写入失败次数。
2. 明确标记 audit_degraded。
3. 最终报告显示审计完整性异常。
4. 不允许在审计数据不完整时宣称统计完全准确。
5. 不允许日志故障绕过 SUBMITTING 持久化。
6. 不允许为了写日志破坏现有报名安全机制。

## 八、任务五：报告兼容性

要求：

- 保留现有控制台报告。
- 保留 Markdown 报告。
- 保留旧版 JSONL 读取兼容。
- 不改变用户现有配置文件格式。
- 旧报告中已有字段尽可能保持兼容。
- 新字段采用明确名称。

可以增加报告 Schema Version，但不要无必要地修改现有状态数据库结构。

## 九、任务六：历史真实数据回放

使用 2026-11-08 的真实抢课记录作为回归基准。

必须保证：

- 列表 HTTP 失败：18 次。
- 失败状态码分布：-1 为 8 次、500 为 8 次、502 为 1 次、503 为 1 次。
- 真实报名提交：6 次。
- UNKNOWN 复核：2 次。
- 最终成功报名：2 节。
- 明确满员失败：1 次。
- UNKNOWN 结果：3 次。

注意：

以上为历史记录中已确认的基准指标。

不得将历史数据推断为当前代码实际运行结果。

## 十、必须新增的测试

R01：报告 Submission 与 Verification 分离。

R02：HTTP 失败与熔断次数分离。

R03：最终对账成功输出正确报名数量。

R04：最终对账失败输出 UNVERIFIED。

R05：最终对账数据不完整输出 UNVERIFIED。

R06：存在 UNKNOWN 时不能输出 SUCCESS。

R07：审计日志写入失败时报告显示 audit_degraded。

R08：不同 run_id 的报告互不混淆。

R09：历史 JSONL 格式兼容。

R10：重复 event_id 不会重复累计。

R11：本地状态与服务端对账结果不一致时明确提示。

R12：历史真实抢课记录回放结果保持一致。

R13：控制台与 Markdown 报告使用相同统计数据。

R14：缺失 request_id 的历史事件仍能正常回放。

R15：审计日志存在未配对 HTTP started 事件时报告显示异常。

## 十一、实施顺序

Phase 4.0：审查 ResultReporter 现有计数器。

Phase 4.1：建立旧指标与事件指标映射。

Phase 4.2：迁移运行统计。

Phase 4.3：迁移 HTTP 和熔断统计。

Phase 4.4：接入最终对账状态。

Phase 4.5：增加审计完整性检测。

Phase 4.6：统一控制台与 Markdown 报告。

Phase 4.7：历史数据回放和全量测试。

每个阶段结束后运行相关测试并检查 Git Diff。

## 十二、禁止事项

- 不允许将 UNKNOWN 当成 FAILED。
- 不允许将最终对账失败当成零报名。
- 不允许用本地 Submission 事件替代服务端最终报名数量。
- 不允许把 Verification 计入 Submission。
- 不允许把 HTTP 失败次数计为熔断次数。
- 不允许静默隐藏审计日志写入失败。
- 不允许删除旧版日志兼容能力。
- 不允许重构 Phase 2 状态机。
- 不允许改变 signUp 单次提交机制。
- 不允许访问真实抢课接口。

## 十三、最终交付

必须提供：

1. 修改文件清单。
2. ResultReporter 新旧统计映射。
3. 最终报告示例。
4. SUCCESS / PARTIAL_SUCCESS / FAILED / UNVERIFIED 判定说明。
5. 历史数据回放结果。
6. R01–R15 测试结果。
7. 全量测试结果。
8. Git Diff。
9. 尚存风险。

未经用户授权，不要合并 main。