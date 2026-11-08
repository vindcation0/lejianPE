# 乐健体育第四轮优化 Phase 6：Preflight 抢课前健康检查系统

## 一、项目当前状态

Phase 0：问题确认完成。

Phase 1：独立熔断器和请求可靠性完成。

Phase 2：UNKNOWN 安全屏障、多进程锁、SQLite 原子预留完成。

Phase 3：统一审计事件体系完成。

Phase 4：ResultReporter 和最终对账体系完成。

Phase 5：checkConflict 智能降级完成。

当前测试基线：412/412 PASS。

Phase 5 分支：

phase5/check-conflict-fallback

本阶段进入 Phase 6。

## 二、核心目标

建立独立的 PreflightChecker，在正式抢课前执行全面的健康检查。

解决：

1. 抢课开始后才发现 Token 失效。
2. 活动列表接口异常。
3. checkConflict 处于 OPEN。
4. 本地报名快照不完整或过期。
5. StateStore 不可用。
6. 存在 UNKNOWN 等未决报名状态。
7. 多进程锁不可用。
8. 审计日志不可写。
9. 系统时间偏差。
10. 每日、每周配额异常。

本阶段只负责检查、诊断和准备。

不得调用真实 signUp 接口。

## 三、重点检查文件

- lejian.py
- core/conflict_checker.py
- core/circuit_breaker.py
- core/state_store.py
- core/registration_manager.py
- core/audit_logger.py
- core/result_reporter.py
- 认证初始化相关模块

先梳理现有调用链。

优先复用现有实现，不要重复造轮子。

建议新增：

core/preflight.py

tests/test_preflight.py

## 四、任务一：认证健康检查

检查：

- Token 是否存在。
- Token 是否过期。
- 当前认证用户是否明确。
- session_scope_id 是否有效。
- Token 刷新机制是否正常。
- 是否存在认证异常。

要求：

1. 尽可能复用已有 AuthInitializer。
2. 需要刷新 Token 时，使用现有安全刷新机制。
3. 不允许在多个检查任务中并发刷新 Token。
4. 刷新失败必须 BLOCKED。
5. 不输出 Token 明文。
6. Token 刷新后，必须重新验证 ActivityList 快照上下文。

## 五、任务二：网络健康检查

分别检查：

### activities

- 接口是否可用。
- 返回数据结构是否正确。
- 是否存在分页异常。
- 熔断器当前状态。
- 最近请求失败情况。

### checkConflict

- 独立健康状态。
- NORMAL / OPEN / HALF_OPEN。
- 是否使用 DEGRADED_LOCAL。
- 本地降级数据是否有效。

要求：

1. 不共用两个接口的熔断器。
2. 不进行高频健康请求。
3. HALF_OPEN 只允许一次探测。
4. 不修改现有熔断器状态转换逻辑。
5. 认证和权限错误不能解释为网络健康问题。

## 六、任务三：报名快照检查

复用 Phase 5 ActivityList。

检查：

- complete=True。
- fetched_at 有效。
- 快照未超过允许时效。
- run_id 匹配。
- session_scope_id 匹配。
- user_id 匹配。
- 没有快照后的本地状态变化。
- 所有分页完整。

如果快照失效：

允许尝试刷新活动列表。

刷新失败：

BLOCKED。

禁止使用旧快照继续放行。

## 七、任务四：StateStore 健康检查

检查：

1. 数据库文件是否可访问。
2. SQLite 是否可读。
3. SQLite 是否可写。
4. 当前事务是否正常。
5. 是否存在 SUBMITTING。
6. 是否存在 UNKNOWN。
7. 是否存在 VERIFYING。
8. 是否存在 RETRYABLE。
9. 是否有损坏或无法识别的状态记录。

建议使用独立临时事务测试数据库写入能力。

测试完成必须回滚，不允许污染真实报名状态。

状态判定：

- UNKNOWN：BLOCKED。
- SUBMITTING：BLOCKED。
- VERIFYING：BLOCKED。
- RETRYABLE：允许继续，但必须重新校验规则和配额。

不修改 Phase 2 状态转换逻辑。

## 八、任务五：并发与锁检查

检查：

- InstanceLock 是否正常。
- 是否存在其他抢课进程。
- SQLite 原子预留是否可用。
- 锁文件目录是否可写。
- 锁是否能够正常释放。

要求：

1. 不能通过健康检查抢占正式运行锁后不释放。
2. 不能删除其他进程持有的锁文件。
3. 检测到其他进程正在执行时，应明确报告。
4. 不允许在锁状态不明确时输出 READY。

## 九、任务六：配额和业务规则检查

复用现有规则引擎。

检查：

- 当前教学周。
- 每日报名限制。
- 每周报名限制。
- 特殊活动规则。
- 已报名课程数量。
- 当前剩余配额。
- 是否存在规则配置错误。

不重复实现 Phase 5 LocalConflictChecker。

无法确认配额时 BLOCKED。

## 十、任务七：审计系统检查

检查：

- AuditLogger 是否可用。
- 当前 run_id 是否存在。
- JSONL 是否可写。
- 当前运行是否 audit_degraded。
- 是否存在历史未解决的运行状态。

使用独立临时文件测试写入能力。

不得将测试事件写入正式审计日志。

关键审计系统不可用时，不得输出 READY。

## 十一、任务八：系统时间检查

检查：

- 本地时间是否合理。
- 时间戳是否存在明显异常。
- 是否能够获取可靠的服务器时间参考。
- 本地与服务器时间偏差。

建议设置可配置的时间偏差阈值。

如果服务器时间不可获取，不要直接假定时间正确，应明确标记 UNKNOWN 或 WARNING，并根据抢课时间敏感性决定是否阻断。

不能通过修改系统时间自动修复。

## 十二、任务九：最终状态判定

只允许三个状态：

### READY

所有 P0 检查通过。

正常使用服务端检查模式。

### DEGRADED_READY

满足：

- 认证有效。
- StateStore 正常。
- 没有 UNKNOWN。
- 快照完整、新鲜且上下文匹配。
- 本地冲突检查可用。
- 原子预留可用。
- 审计系统满足运行要求。

允许存在 checkConflict 服务端故障，但必须明确标记 LOCAL FALLBACK。

### BLOCKED

任意关键安全检查失败：

- Token 无效。
- UNKNOWN。
- SUBMITTING。
- VERIFYING。
- 数据不完整。
- 快照过期且刷新失败。
- StateStore 不可用。
- 锁状态不明确。
- 审计关键故障。
- 无法确认关键配额。

必须阻止启动。

## 十三、任务十：报告与审计

建议输出：

preflight_report.json

以及控制台摘要。

报告包括：

- run_id
- 检查开始时间
- 检查结束时间
- 当前用户范围标识
- 当前会话范围标识
- 每个检查项的状态
- 检查耗时
- 失败原因
- 降级模式
- 最终启动判定

禁止输出：

- Token
- 密码
- 原始学号
- 敏感认证信息

增加统一审计事件：

- preflight_started
- preflight_check_completed
- preflight_degraded
- preflight_blocked
- preflight_ready

## 十四、必须新增的测试

P01：正常环境输出 READY。

P02：checkConflict OPEN，但本地数据有效，输出 DEGRADED_READY。

P03：Token 无效输出 BLOCKED。

P04：Token 刷新失败输出 BLOCKED。

P05：刷新 Token 后旧快照失效。

P06：activities 接口失败且无有效快照，BLOCKED。

P07：报名列表分页不完整，BLOCKED。

P08：快照超过有效期且刷新失败，BLOCKED。

P09：存在 UNKNOWN，BLOCKED。

P10：存在 SUBMITTING，BLOCKED。

P11：存在 VERIFYING，BLOCKED。

P12：仅存在 RETRYABLE，重新校验后允许继续。

P13：StateStore 不可读，BLOCKED。

P14：StateStore 不可写，BLOCKED。

P15：InstanceLock 不可用，BLOCKED。

P16：存在其他抢课进程时明确报告。

P17：配额数据不完整，BLOCKED。

P18：特殊活动规则无法识别，BLOCKED。

P19：审计日志不可写，BLOCKED。

P20：审计日志测试不污染正式日志。

P21：系统时间偏差超出阈值，按规则阻断。

P22：activities 和 checkConflict 的健康状态互不影响。

P23：HALF_OPEN 探测并发仍然只有一次。

P24：Preflight 不调用 signUp。

P25：Preflight 不改变真实报名状态。

P26：重复执行 Preflight 不会产生数据库脏写。

P27：Token 刷新并发受到保护。

P28：最终报告 run_id 与本次运行一致。

## 十五、实施顺序

Phase 6.0：审查现有模块和调用关系。

Phase 6.1：认证健康检查。

Phase 6.2：网络和熔断状态检查。

Phase 6.3：ActivityList 快照检查。

Phase 6.4：StateStore 与并发锁检查。

Phase 6.5：配额、审计和时间检查。

Phase 6.6：最终 READY 判定。

Phase 6.7：统一报告与审计。

Phase 6.8：全量回归测试。

每一步先检查、再修改。

不要一次性重写所有模块。

## 十六、禁止事项

- 不允许调用真实 signUp。
- 不允许为了健康检查执行真实报名。
- 不允许修改 Phase 2 状态机。
- 不允许绕过 UNKNOWN。
- 不允许绕过 InstanceLock。
- 不允许绕过 SQLite 原子预留。
- 不允许修改 Phase 5 熔断器行为。
- 不允许将未知状态默认为健康。
- 不允许自动修改生产配置。
- 不允许合并 main。
- 不允许访问真实抢课环境。

## 十七、最终交付

1. PreflightChecker 架构。
2. 检查项清单。
3. READY / DEGRADED_READY / BLOCKED 判定树。
4. P01–P28 测试结果。
5. 全量测试结果。
6. 控制台报告示例。
7. JSON 报告示例。
8. Git Diff。
9. 尚存风险。

未经用户授权，不得调用真实报名接口。