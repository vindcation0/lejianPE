# Phase 5 最终封版检查：本地数据完整性与降级安全

## 一、当前进度

Phase 0–4 已完成。

Phase 5 主体实现已完成：

- 独立 checkConflict 熔断器。
- LocalConflictChecker。
- 自动降级。
- 服务端恢复探测。
- 配额与时间冲突保护。
- 统一审计事件。

当前测试基线：

390/390 PASS。

本次只进行最终边界检查，不进行大规模重构。

## 二、任务一：修复 complete 默认值问题

检查：

- core/conflict_checker.py
- LocalConflictChecker
- 本地报名数据获取调用链。

当前报告使用：

getattr(items, "complete", True)

请检查是否会将缺少 complete 属性的数据错误认定为完整。

要求：

1. complete 必须有明确证据。
2. complete 缺失时必须 BLOCKED。
3. complete=False 必须 BLOCKED。
4. 不允许默认 True。
5. 不能通过普通 list 类型自动推断分页完整性。
6. 保持现有 ActivityList 兼容。

新增：

C27：items 缺少 complete 属性，必须 BLOCKED。

C28：分页不完整，必须 BLOCKED。

## 三、任务二：修正空列表的语义

检查：

items 非空这一限制。

明确区分：

- None：无有效数据，BLOCKED。
- 格式错误：BLOCKED。
- 空列表且 complete=True：有效的完整空数据。
- 非空列表且 complete=True：有效完整数据。
- 空列表但无法证明 complete：BLOCKED。

如果空列表已经经过成功的完整服务端查询，应允许进入本地冲突评估。

新增：

C29：完整且可信的空报名列表允许本地评估。

C30：空列表但 complete=False，必须 BLOCKED。

## 四、任务三：增加本地数据快照有效性检查

检查本地数据是否具有：

- 获取时间。
- run_id。
- 数据来源。
- 完整性标记。
- 用户范围一致性。

要求：

1. 过期数据不能直接放行。
2. 上一次运行的数据不能无条件用于当前运行。
3. 数据获取后如果发生影响报名状态的变化，需要重新验证。
4. 无法确认数据新鲜度时 BLOCKED。
5. 不允许通过默认时间或默认 run_id 伪造数据有效性。

建议使用当前项目已有的运行标识与审计机制。

新增：

C31：本地报名数据超过有效期，必须 BLOCKED。

C32：本地数据 run_id 不匹配，必须 BLOCKED。

C33：本地数据获取后状态发生变化，必须重新验证。

具体有效期由现有业务流程决定，不要凭空设置激进的超短时间。

## 五、任务四：验证 401、403、400 的业务处理

检查：

- check_conflict_detailed()
- ConflictManager
- validate_rules()

要求：

1. 401 不进入基础设施熔断，但必须先恢复认证，不能盲目使用本地降级继续提交。
2. 403 必须明确阻断。
3. 400 必须记录参数异常。
4. 5xx、网络异常、超时继续执行现有降级策略。
5. 不改变 activities 独立熔断器。

新增：

C34：401 认证未恢复时，禁止进入报名提交。

C35：403 权限异常时，禁止报名提交。

C36：400 参数异常不累计基础设施熔断次数。

## 六、任务五：检查 RETRYABLE 与本地降级兼容性

结合 Phase 4 的状态定义：

SUBMITTING、UNKNOWN、VERIFYING 属于不确定状态。

RETRYABLE 表示已经确认未报名，但需要重新校验。

检查 get_unresolved_records() 是否将 RETRYABLE 一并阻断。

要求：

1. 不能绕过 UNKNOWN 安全屏障。
2. RETRYABLE 不能被错误解释为 UNKNOWN。
3. RETRYABLE 只有重新校验规则、配额和本地数据后才能继续。
4. 不修改 Phase 2 状态转换逻辑。

新增：

C37：只有 RETRYABLE 时，完成重新校验后允许进入本地评估。

C38：存在 UNKNOWN 时，即使同时存在 RETRYABLE，也必须 BLOCKED。

## 七、实施要求

1. 先检查代码和实际调用链。
2. 输出问题及最小修改方案。
3. 只修改必要文件。
4. 保留 C01–C26。
5. 保留原有 390 项测试。
6. 不修改 Phase 1、Phase 2 核心安全逻辑。
7. 不访问真实报名接口。
8. 所有测试使用 Mock 和临时数据库。

## 八、最终交付

1. C27–C38 测试结果。
2. 全量测试结果。
3. 本地数据完整性最终判定规则。
4. 本地数据新鲜度判定规则。
5. 401、403、400 的处理说明。
6. RETRYABLE 与 UNKNOWN 的兼容说明。
7. Git Diff。
8. 剩余风险。

完成后正式冻结 Phase 5，进入 Phase 6。

未经用户授权，不要合并 main。