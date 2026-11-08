# Phase 2 最终封版检查：原子预留幂等性

当前 Phase 2 补充验收已完成：

- 全量测试：313/313 通过
- 多进程测试：M01–M07 全部通过
- UNKNOWN 测试：U01–U24 全部通过

现在只进行最后一轮小范围检查，不进行架构重构。

## 任务一：检查相同 activityId 重复预留

重点检查：

- core/state_store.py
- core/registration_manager.py
- reserve_submitting()
- record_submitting()

当前报告展示的 SQL：

SELECT *
FROM registration_records
WHERE status IN (?, ?, ?)
AND activity_id != ?
LIMIT 1

请重点检查排除当前 activity_id 后，是否可能允许同一个 UNKNOWN 或 SUBMITTING 活动再次预留。

要求：

1. 当前活动为 SUBMITTING 时，禁止再次预留。
2. 当前活动为 UNKNOWN 时，禁止再次预留。
3. 当前活动为 VERIFYING 时，禁止再次预留。
4. 当前活动为 SUCCESS 时，禁止再次预留。
5. 当前活动为 RETRYABLE 时，只有完成重新规则校验后才能允许预留。
6. 其他活动存在未决记录时，仍然禁止预留。
7. 所有检查必须在 SQLite 原子事务内完成。

新增测试：

M08：相同 activityId 连续两次 reserve_submitting，第二次必须拒绝。

M09：当前 activityId 为 UNKNOWN，再次预留必须拒绝。

M10：当前 activityId 为 RETRYABLE，经过重新规则校验后可以重新预留。

M11：当前 activityId 为 SUCCESS，必须拒绝再次预留。

## 任务二：检查 SQLite 事务边界

检查 reserve_submitting() 调用 record_submitting() 时：

- 是否使用相同 connection。
- 是否存在内部 commit。
- 是否存在独立 connection。
- 异常发生后是否完整 rollback。

通过测试证明预留操作具有真正的事务原子性。

## 任务三：检查真实入口锁集成

检查 cmd_grab()：

- 是否在认证和业务请求之前获取 InstanceLock。
- 获取失败是否直接退出。
- 锁是否覆盖完整抢课执行周期。
- 正常退出和异常退出是否都能释放资源。

增加一个实际入口级测试，证明第二个进程被拒绝后 HTTP 请求次数为零。

## 任务四：交付

只修改必要文件。

保留已有 313 项测试。

提供：

1. M08–M11 测试结果。
2. InstanceLock 入口集成测试结果。
3. SQLite 事务检查结果。
4. 完整 Git Diff。
5. 全量测试结果。

不得访问真实报名接口。

本次完成后正式封版 Phase 2，进入 Phase 3。

未经用户授权，不要合并 main。