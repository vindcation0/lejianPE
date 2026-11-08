# 乐健体育第四轮优化 Phase 3：统一审计日志与事件统计

## 一、项目当前进度

Phase 0：问题确认完成。

Phase 1：熔断器修复完成。

Phase 2：UNKNOWN 安全屏障、SQLite 原子预留、多进程保护正式封版。

当前全量测试基线：

319/319 PASS。

本阶段正式进入 Phase 3。

## 二、本阶段核心目标

建立统一、准确、可追溯的事件审计体系。

解决过去实际抢课中出现的统计问题：

1. 实际活动列表请求失败次数无法准确统计。
2. 真实 signUp 次数与 UNKNOWN 复核次数混淆。
3. 故障事件次数与 HTTP 请求失败次数混淆。
4. 不同模块重复记录相同业务事件。
5. 最终报告无法准确还原一次抢课的完整执行过程。

本阶段重点是建立可信的原始事件数据。

暂时不要大规模重构 ResultReporter，最终统计展示留给 Phase 4。

## 三、重点检查文件

- core/audit_logger.py
- core/request_manager.py
- core/registration_manager.py
- core/reconciliation.py
- core/circuit_breaker.py
- core/result_reporter.py
- lejian.py

先检查当前实现，尽量复用已有 AuditLogger。

不要重新设计已经封版的 UNKNOWN 状态机。

不要修改 Phase 1 和 Phase 2 已经验证通过的安全机制。

## 四、任务一：建立统一事件模型

建议统一事件结构：

{
"schema_version": 1,
"event_id": "unique-event-id",
"run_id": "unique-run-id",
"correlation_id": "business-operation-id",
"timestamp": "ISO-8601",
"event_type": "submission",
"event_name": "submission_started",
"activity_id": "1001",
"status": "SUBMITTING",
"reason": null,
"http_status": null,
"duration_ms": null,
"source": "registration_manager"
}

字段可以根据当前架构调整，但必须保证：

- 每个事件具有唯一 event_id。
- 同一次程序运行使用统一 run_id。
- 同一次报名业务使用统一 correlation_id。
- 活动 ID 可以关联同一活动的多个事件。
- 时间戳统一使用 ISO-8601。
- 事件类型有明确枚举。
- 状态变化有明确记录。
- 不同事件不能使用模糊的统一计数器。

敏感信息必须脱敏。

禁止记录 Token、密码、手机号、学号等敏感信息。

## 五、任务二：严格区分五类事件

### 1. Candidate

记录候选评估：

- candidate_discovered
- candidate_check_started
- candidate_accepted
- candidate_rejected

规则拦截不算真实报名提交。

### 2. Submission

记录真实报名：

- submission_started
- submission_response_received
- submission_succeeded
- submission_failed
- submission_unknown

关键要求：

只有实际调用 submit_once() 时，才产生真实提交请求统计。

一次 signUp 调用最多只能对应一次 Submission 请求计数。

规则拦截、候选评估、UNKNOWN 复核不能增加 Submission 数量。

signUp 禁止自动重试。

### 3. Verification

记录 UNKNOWN 复核：

- verification_started
- verification_request_succeeded
- verification_request_failed
- verification_confirmed_registered
- verification_confirmed_unregistered
- verification_remains_unknown

每次实际复核请求单独统计。

复核不能增加 Submission 次数。

### 4. Reconciliation

记录全局对账：

- reconciliation_started
- reconciliation_succeeded
- reconciliation_failed
- reconciliation_incomplete

必须记录：

- 对账请求次数。
- 对账结果完整性。
- 对账确认的报名数量。
- 未解决 UNKNOWN 数量。

对账失败不能被解释为零报名。

### 5. Infrastructure

记录底层 HTTP 和服务器故障：

- http_request_started
- http_request_succeeded
- http_request_failed
- circuit_open
- circuit_half_open
- circuit_closed
- infrastructure_recovered

必须明确区分：

HTTP 请求失败次数与故障事件次数。

一次故障持续期间发生多次 HTTP 失败，必须记录每次请求失败，但故障开始事件不能重复创建。

## 六、任务三：确保 HTTP 请求统计准确

重点检查：

Client.activities()
RequestManager
冲突检查接口
Token 刷新接口
signUp 接口

要求：

1. 每次实际发出的 HTTP 请求都能够准确记录。
2. 请求开始和结束能够关联。
3. 记录 HTTP 状态码。
4. 记录异常类型。
5. 记录请求耗时。
6. 请求失败不能因为返回 complete=False 而丢失统计。
7. 内部重试必须分别记录每次底层请求。
8. 外层轮询不能重复累计内部请求。
9. 熔断器状态转换与 HTTP 请求统计分离。

特别注意：

一次外层轮询如果发生三次底层 HTTP 请求：

HTTP Request = 3。

不能因为它们属于同一次活动列表轮询，就只统计一次。

## 七、任务四：统一 AuditLogger

检查现有 AuditLogger 是否存在：

- 多个模块各自写 JSONL。
- 同一个事件被重复记录。
- 事件字段不一致。
- 写日志异常被静默吞掉。
- 缺少事件关联 ID。

尽量统一由 AuditLogger 负责事件持久化。

建议使用 JSONL 格式，一行一个事件。

要求：

1. 写入失败必须有明确处理和日志告警。
2. 不允许静默丢失关键报名状态事件。
3. 不允许日志故障导致程序绕过 SUBMITTING 持久化。
4. 日志字段保持兼容。
5. 旧日志可以继续读取。
6. 不允许记录敏感认证信息。
7. 不要因为日志重复而重复累计业务统计。

如果关键审计事件无法写入，应明确区分普通诊断日志与报名状态安全日志，不能为了记录日志而破坏现有报名安全机制。

## 八、任务五：与 ResultReporter 分离

本阶段只建立可信事件源。

不要同时大规模重构 ResultReporter。

确保后续可以从事件日志准确计算：

- 候选数量。
- 实际 Submission 数量。
- Verification 数量。
- Reconciliation 数量。
- HTTP 请求总数。
- HTTP 失败总数。
- 各状态码失败次数。
- 故障持续时间。
- 熔断次数。
- UNKNOWN 数量。

每个指标都必须能够追溯到原始事件。

## 九、任务六：使用真实抢课记录进行回放测试

重点使用 2026-11-08 的真实抢课记录作为历史测试依据。

需要覆盖：

1. 18 次活动列表请求失败。
2. 6 次真实报名提交。
3. 2 次 UNKNOWN 复核记录。
4. checkConflict 接口异常。
5. 熔断器开启和恢复。
6. UNKNOWN 最终复核结果。

注意：

历史记录中如果某个事件的真实 HTTP 调用次数无法确认，不要自行补造。

必须区分已确认数据与推断数据。

建议将真实记录整理成脱敏 fixture，通过 Mock 事件回放验证统计结果。

禁止连接真实抢课接口。

## 十、必须新增的测试

A01：每次 HTTP 请求都有独立事件。

A02：连续多次 HTTP 失败准确统计。

A03：Submission 与 Verification 完全分离。

A04：规则拦截不增加 Submission。

A05：一次 submit_once 对应一次提交事件。

A06：HTTP 失败事件与故障开始事件分离。

A07：内部重试准确记录底层请求次数。

A08：同一个 correlation_id 能关联完整业务过程。

A09：不同 run_id 的事件不会混淆。

A10：JSONL 写入失败能够明确告警。

A11：Token 等敏感信息不会进入审计日志。

A12：旧版日志格式仍然可以读取。

A13：同一事件重复处理不会重复累计业务统计。

A14：活动列表 complete=False 不会丢失 HTTP 失败统计。

A15：历史抢课记录回放结果符合已确认的原始数据。

## 十一、实施顺序

Phase 3.0：审查 AuditLogger 和所有事件写入位置。

Phase 3.1：建立统一事件 Schema。

Phase 3.2：区分 Candidate、Submission、Verification、Reconciliation。

Phase 3.3：接入底层 HTTP 请求审计。

Phase 3.4：统一故障和熔断事件。

Phase 3.5：历史抢课日志回放。

Phase 3.6：全量测试和 Git Diff 审查。

每个阶段完成后运行相关测试。

不要一次性重构所有模块。

## 十二、禁止事项

- 不允许修改 signUp 的单次提交安全机制。
- 不允许改变 UNKNOWN 状态转换规则。
- 不允许移除多进程锁。
- 不允许改变 SQLite 原子预留逻辑。
- 不允许把 Verification 统计为 Submission。
- 不允许把故障事件次数当成 HTTP 失败次数。
- 不允许静默丢弃关键审计事件。
- 不允许记录敏感认证信息。
- 不允许为了通过测试删除原有测试。
- 不允许调用真实报名接口。
- 不允许在本阶段提前实施 Phase 4 的报告重构。

## 十三、最终交付

必须提供：

1. 事件 Schema 文档。
2. 修改文件清单。
3. 各模块事件写入位置。
4. Submission 和 Verification 统计边界说明。
5. HTTP 请求统计说明。
6. 历史抢课记录回放结果。
7. A01–A15 测试结果。
8. 全量测试结果。
9. Git Diff。
10. 尚存风险。

最终需要证明：

同一次抢课过程中的候选评估、真实报名、UNKNOWN 复核、服务端对账和底层 HTTP 请求，都可以独立统计，并且能够通过事件 ID 和关联 ID 完整追溯。

未经用户授权，不要合并 main。