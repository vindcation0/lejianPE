# 乐健体育抢课系统第四轮优化 Phase 5：checkConflict 智能降级

## 一、当前项目进度

Phase 0：问题确认完成。

Phase 1：活动列表熔断器修复完成。

Phase 2：UNKNOWN 安全屏障、多进程锁、SQLite 原子预留正式封版。

Phase 3：统一审计事件体系正式封版。

Phase 4：ResultReporter 重构、审计完整性和 run_id 严格校验正式封版。

当前全量测试基线：364/364 PASS。

现在正式进入 Phase 5。

## 二、本阶段核心目标

解决真实抢课中 checkConflict 接口持续异常的问题。

当前系统可以通过：

check_conflict: false

关闭服务端冲突检查，但这种处理方式会失去冲突保护。

本阶段需要实现：

1. checkConflict 独立健康管理。
2. 服务端接口异常时自动进入本地降级。
3. 本地数据完整时使用本地规则检查。
4. 本地数据不完整时安全阻断。
5. 服务端恢复后自动恢复正常检查。
6. 完整记录降级和恢复过程。

核心原则：

允许降级，不允许失去安全保护。

## 三、重点检查文件

- core/request_manager.py
- core/circuit_breaker.py
- core/registration_manager.py
- core/audit_logger.py
- core/result_reporter.py
- core/state_store.py
- lejian.py
- 配置文件及相关测试

先检查现有架构，尽量复用已有熔断器和事件体系。

不要直接重写已有模块。

## 四、任务一：checkConflict 独立健康管理

为 checkConflict 建立独立熔断器。

建议状态：

NORMAL
OPEN
HALF_OPEN
DEGRADED_LOCAL

要求：

1. 不与 activities 共用熔断状态。
2. checkConflict 连续服务器故障后独立进入 OPEN。
3. OPEN 时不继续发送正常冲突检查请求。
4. 冷却结束后进入 HALF_OPEN。
5. HALF_OPEN 只允许一次只读健康探测。
6. 完整、有效的服务端冲突检查响应才能恢复 NORMAL。
7. 不完整响应不能恢复。
8. 认证异常、权限异常和参数异常不能误认为服务器基础设施故障。
9. 保留现有 CircuitBreaker 已验证的故障分类规则。

注意：

DEGRADED_LOCAL 是业务检查模式，不应该简单等同于基础设施熔断状态。

请明确区分健康状态与业务降级状态，避免状态机混乱。

## 五、任务二：实现本地冲突检查器

建立独立的 LocalConflictChecker。

优先复用项目现有课程时间解析和规则判断逻辑。

检查内容：

1. 已报名课程的时间范围。
2. 候选课程的时间范围。
3. 日期重叠。
4. 星期重叠。
5. 跨天课程。
6. 不同教学周。
7. 特殊活动规则。
8. 每日报名限制。
9. 每周报名限制。
10. 已报名但尚未同步的课程状态。

要求：

- 不重复实现已有配额规则。
- 尽量复用现有规则引擎。
- 不能只根据课程名称判断冲突。
- 不能只检查开始时间。
- 必须正确处理课程结束时间。
- 时间解析失败时不能默认无冲突。
- 未知课程类型不能被默认归为普通活动。

## 六、任务三：本地数据完整性检查

这是本阶段最重要的安全要求。

在使用本地降级检查之前，必须确认：

1. 已报名课程列表获取成功。
2. 数据分页完整。
3. 数据格式正确。
4. 每条课程都有必要的时间信息。
5. 课程类型可以识别。
6. 当前教学周信息明确。
7. 本地报名状态不存在未解决 UNKNOWN。
8. 没有正在进行的全局对账。
9. 本地配额数据可信。

只有全部满足，才允许进入本地冲突判断。

如果任何一项不满足：

返回 BLOCKED。

不能将缺失数据当成空列表。

不能将 UNKNOWN 当成未报名。

不能因服务端冲突接口故障而绕过 Phase 2 安全屏障。

## 七、任务四：自动降级与恢复流程

建议实现：

NORMAL：

使用服务端 checkConflict。

服务端连续基础设施故障达到阈值：

进入 OPEN。

冷却结束：

进入 HALF_OPEN。

健康探测成功：

恢复 NORMAL。

健康探测失败：

重新 OPEN。

本地数据完整且可信：

允许 DEGRADED_LOCAL。

本地数据不完整：

进入 BLOCKED。

注意：

- 401 交给认证处理机制。
- 403 明确提示权限异常。
- 400 明确提示参数异常。
- 5xx、网络超时、连接失败按照基础设施故障处理。
- 不要对客户端错误执行无意义的健康探测。
- 不要因为 checkConflict 故障打开 activities 熔断器。

## 八、任务五：安全提交边界

本地降级不能直接绕过报名管理器。

无论使用服务端检查还是本地检查，最终都必须经过：

1. UNKNOWN 全局屏障。
2. InstanceLock。
3. SQLite 原子预留。
4. 每日、每周配额检查。
5. SUBMITTING 持久化。
6. 单次 submit_once()。

本阶段不允许修改 signUp 单次提交机制。

如果本地数据不完整或无法确定冲突：

禁止提交。

不能使用：

except Exception:
return False

将异常伪装成无冲突。

所有异常必须有明确分类。

## 九、任务六：统一审计事件

接入现有 AuditLogger。

建议增加事件：

- conflict_check_started
- conflict_check_succeeded
- conflict_check_failed
- conflict_check_degraded
- local_conflict_check_started
- local_conflict_check_succeeded
- local_conflict_detected
- local_conflict_check_blocked
- conflict_service_recovered

记录：

- run_id
- correlation_id
- request_id（适用时）
- 检查模式
- 故障原因
- 降级开始时间
- 降级结束时间
- 本地数据完整性
- 最终判断

不得记录 Token 或个人敏感信息。

不能把一次本地检查计为一次 HTTP 请求。

不能把 checkConflict 的 HTTP 失败计入 activities 的 HTTP 失败统计。

## 十、任务七：配置兼容性

检查现有：

check_conflict

配置项的实际语义。

要求：

1. 保留旧配置文件兼容。
2. 明确 false 是否代表完全禁用冲突检查。
3. 如果用户显式禁用服务端检查，不能静默改变原有配置含义。
4. 不允许因为配置关闭服务端检查就同时关闭本地安全检查。
5. 输出清晰的启动配置摘要。
6. 不擅自修改用户生产配置。

## 十一、必须新增的测试

C01：checkConflict 连续 5xx 独立触发熔断。

C02：checkConflict 故障不影响 activities 熔断器。

C03：OPEN 状态禁止正常冲突请求。

C04：HALF_OPEN 只允许一次探测。

C05：HALF_OPEN 成功恢复 NORMAL。

C06：HALF_OPEN 失败重新 OPEN。

C07：401 不累计基础设施故障。

C08：403 不累计基础设施故障。

C09：本地完整数据无冲突时允许继续评估。

C10：本地完整数据存在冲突时阻断。

C11：本地课程列表不完整时阻断。

C12：本地课程时间解析失败时阻断。

C13：存在 UNKNOWN 时本地降级阻断。

C14：存在 SUBMITTING 时本地降级阻断。

C15：跨天课程冲突判断正确。

C16：不同教学周课程不会错误判定冲突。

C17：每日配额仍然有效。

C18：每周配额仍然有效。

C19：特殊活动规则不被绕过。

C20：本地数据恢复完整后允许重新评估。

C21：服务端恢复后切回 NORMAL。

C22：本地降级事件准确记录。

C23：checkConflict 请求失败独立统计。

C24：本地检查不增加 HTTP 请求次数。

C25：最终 signUp 仍然只有一次调用。

C26：配置文件旧格式兼容。

所有测试使用 Mock 和临时数据库，不允许调用真实抢课接口。

## 十二、实施顺序

Phase 5.0：检查 checkConflict 当前调用链和配置。

Phase 5.1：独立健康管理和熔断器。

Phase 5.2：本地冲突检查器。

Phase 5.3：本地数据完整性验证。

Phase 5.4：自动降级和恢复。

Phase 5.5：统一审计事件。

Phase 5.6：完整回归测试。

每一步先检查，再修改。

每一步完成后执行相关测试。

不要一次性重构所有模块。

## 十三、禁止事项

- 不允许把服务端异常直接解释为无冲突。
- 不允许不完整本地数据进入正常降级。
- 不允许绕过 UNKNOWN 安全屏障。
- 不允许绕过 SQLite 原子预留。
- 不允许绕过 InstanceLock。
- 不允许改变 signUp 单次提交机制。
- 不允许修改已封版的 Phase 1、Phase 2 核心逻辑。
- 不允许混淆不同接口的 HTTP 统计。
- 不允许访问真实抢课接口。
- 不允许为了通过测试删除原有测试。

## 十四、最终交付

1. 修改文件清单。
2. checkConflict 状态机图。
3. LocalConflictChecker 设计说明。
4. 本地数据完整性规则。
5. 自动降级与恢复流程。
6. 配置兼容性说明。
7. C01–C26 测试结果。
8. 全量测试结果。
9. 历史 2026-11-08 记录回放结果。
10. Git Diff。
11. 尚存风险。

完成后不得擅自合并 main。

未经用户授权，不得调用真实报名接口。