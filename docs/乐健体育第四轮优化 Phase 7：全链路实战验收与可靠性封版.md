# 乐健体育第四轮优化 Phase 7：全链路实战验收与可靠性封版

## 一、当前项目状态

Phase 0–6 已完成。

Phase 6：

- PreflightChecker 已实现。
- READY / DEGRADED_READY / BLOCKED 三态判定。
- P01–P28 全部通过。
- 全量测试 440/440 PASS。

当前分支：

phase6/preflight-system

正式进入 Phase 7。

本阶段目标不是增加新功能，而是验证所有模块在完整抢课链路中的协作正确性。

## 二、Phase 7 核心目标

建立端到端集成测试体系。

重点验证：

1. Preflight 到正式运行的衔接。
2. 活动列表获取。
3. 冲突检查和降级。
4. 配额校验。
5. 多进程原子预留。
6. 单次报名提交。
7. UNKNOWN 复核。
8. 最终结果对账。
9. 审计统计。
10. 进程重启恢复。

## 三、第一步：先解决三个遗留问题

### F01：activities 熔断后的启动模式

明确：

- activities NORMAL + checkConflict NORMAL。
- activities NORMAL + checkConflict OPEN。
- activities OPEN + 有效缓存。
- 两个接口同时 OPEN。
- activities OPEN + 缓存无效。

区分：

ACTIVITY_CACHE
LOCAL_CONFLICT
SERVER_MODE

不允许将缓存模式错误标记为 READY。

### F02：InstanceLock 时间窗口

Preflight 只负责探测。

正式运行必须重新获取 InstanceLock，并持有整个运行周期。

不能依赖 Preflight 的锁检查结果替代正式锁。

新增并发测试验证两个进程无法同时进入正式抢课流程。

### F03：网络健康状态语义

明确区分：

- 熔断器状态。
- 最近一次请求结果。
- 当前实时健康探测结果。

不得将 CLOSED 直接解释为接口当前健康。

同时核对 Phase 6 的检查项数量，统一文档中的 9 项/10 项统计。

## 四、第二步：建立端到端测试环境

新增：

tests/integration/

建议文件：

- test_full_grab_flow.py
- test_fault_recovery_flow.py
- test_concurrent_grab_flow.py
- test_restart_recovery.py
- test_audit_consistency.py
- test_preflight_integration.py

要求：

1. 所有接口使用 Mock Server。
2. 使用独立临时 SQLite 数据库。
3. 使用独立临时审计目录。
4. 使用独立运行批次 ID。
5. 不访问真实报名环境。
6. 不修改生产配置。
7. 不污染历史数据。

## 五、第三步：完整正常流程

模拟：

Preflight READY
→ 获取活动列表
→ 服务端冲突检查
→ 规则检查
→ SQLite 原子预留
→ signUp 单次提交
→ 服务端成功响应
→ 本地状态更新
→ 最终对账
→ SUCCESS

检查：

- signUp 调用次数严格为 1。
- 不重复预留。
- 最终数据库状态正确。
- HTTP 请求统计准确。
- 审计事件完整。
- ResultReporter 与数据库状态一致。

## 六、第四步：故障注入测试

至少覆盖：

1. activities 500。
2. checkConflict 500。
3. 两个接口同时故障。
4. Token 过期。
5. Token 刷新失败。
6. 提交过程中网络超时。
7. 提交后响应丢失。
8. 服务端成功但本地写入失败。
9. 最终对账失败。
10. 审计写入失败。
11. SQLite 锁冲突。
12. 进程中途退出。

每个场景都必须明确最终状态。

不允许将不确定状态报告为 SUCCESS。

## 七、第五步：多进程与竞争测试

模拟 2、5、10 个进程同时抢课。

验证：

- InstanceLock。
- SQLite 原子预留。
- 配额不超限。
- 同一活动不重复提交。
- 不同活动不会错误互相阻塞。
- 进程退出后锁正常释放。
- 重启后状态能够恢复。

同时检查操作系统进程异常退出、锁超时和 SQLite BUSY 场景。

## 八、第六步：重启恢复测试

模拟：

SUBMITTING
→ 进程被强制终止
→ 重新启动
→ Preflight 检查
→ UNKNOWN 安全屏障
→ 服务端复核
→ 最终状态恢复

覆盖：

- 服务端已经报名。
- 服务端实际未报名。
- 服务端返回不确定。
- 审计日志不完整。
- 本地数据库存在遗留状态。

要求：

不能因为重启而重复提交。

## 九、第七步：建立统一一致性检查器

在集成测试中统一核对：

1. HTTP 实际请求数量。
2. signUp 实际调用数量。
3. 报名业务事件数量。
4. StateStore 状态。
5. UNKNOWN 数量。
6. 配额占用。
7. 审计事件完整性。
8. 最终对账结果。
9. ResultReporter 最终结论。

必须区分：

HTTP 请求、业务事件、报名成功记录和最终复核次数。

不能把一次全局对账解决多个 UNKNOWN 计算成多次 HTTP 请求。

## 十、测试规模建议

新增：

I01–I40：端到端正常流程和故障注入。

I41–I55：多进程和配额竞争。

I56–I70：进程退出与恢复。

I71–I80：审计和结果一致性。

测试必须有明确的输入、故障注入点、预期状态和实际状态。

所有测试可重复执行。

## 十一、最终交付

1. Phase 7 遗留问题修复清单。
2. 完整业务流程图。
3. 端到端测试结果。
4. 多进程压力测试结果。
5. 故障注入结果。
6. 重启恢复结果。
7. 审计与数据库一致性报告。
8. 全量测试结果。
9. Git Diff。
10. 尚存风险。
11. 第四轮最终架构说明。

禁止未经授权访问真实报名接口。

未经用户授权，不得合并 main。

## 十二、最终验收原则

- 不确定状态绝不报告 SUCCESS。
- signUp 不允许重复调用。
- 配额不允许超限。
- UNKNOWN 不允许被绕过。
- 不完整快照不允许放行。
- 不同用户数据不允许混用。
- 审计和结果报告必须可追溯。
- 多进程不能破坏原子性。
- 重启不能造成重复报名。

完成后提交最终报告，等待人工验收。