# 第四轮优化 Phase 5 最终封版检查报告：本地数据完整性与降级安全

> **报告版本**：v1.0 (Phase 5 封版终审版)  
> **实施分支**：`phase5/check-conflict-fallback`  
> **基准代码库**：lejianPE (第四轮系统优化)  
> **验收日期**：2026-11-08  
> **测试状态**：**402 / 402 PASS (100%)**

---

## 一、概述与封版目标

在 Phase 5 首期落地了 `ConflictCircuitBreaker`、`LocalConflictChecker` 与 `ConflictManager`（实现 checkConflict 独立健康熔断与智能降级）后，本次任务执行了针对「本地数据完整性与降级安全」的最终封版边界审查。

根据 `docs/Phase 5 最终封版检查：本地数据完整性与降级安全.md` 的规范要求，本次检查聚焦并彻底修复了 5 大核心安全边界问题：
1. **修复 `complete` 默认值缺陷**：消除 `getattr(items, "complete", True)` 自动推断缺陷，严格要求 `complete` 必须存在显式属性证据，缺失或 `complete=False` 必须阻断。
2. **修正空列表语义**：区分 `None`/格式非法（阻断）与带有 `complete=True` 及新鲜快照的合规空数据（允许进入本地规则判定），消除空列表被盲目误杀或残缺空列表被误放行问题。
3. **增加本地快照新鲜度与时效校验**：新增 `fetched_at` 时间戳有效期限制（默认 60s）、`run_id` 跨运行隔离校验，以及 StateStore 本地状态变更后的时序冲突探测（若本地产生新变更则必须刷新快照）。
4. **规范 401 / 403 / 400 客户端异常处理**：401 未恢复认证时严格阻断报名提交且不盲目切入本地降级；403 权限不足严格阻断；400 业务错误不累计基础设施故障熔断计数。
5. **明确 `RETRYABLE` 与 `UNKNOWN` 的本地降级兼容性**：`RETRYABLE` 为已对账确认未报名的安全可重试状态，允许通过规则评估；但只要存在任何一条 `UNKNOWN`/`SUBMITTING`/`VERIFYING`，绝不绕过安全屏障，严格执行 `BLOCKED`。

---

## 二、本地数据完整性与新鲜度判定规则

### 1. 本地数据完整性判定矩阵 (Local Data Integrity)

| 数据形态 | `complete` 属性 | 快照时间戳 `fetched_at` | 判定结果 | 决策行为 | 对应测试 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `None` | 无 | 无 | ❌ `BLOCKED` | 无法确认报名状态，安全阻断 | C09 |
| 非 list/tuple 格式 | 无 | 无 | ❌ `BLOCKED` | 数据格式非法，安全阻断 | C10 |
| 普通 Python list `[]` / `[...]` | 缺失属性 | 缺失 | ❌ `BLOCKED` | 缺失 `complete` 完整性证据，禁止推断 | **C27** |
| `ActivityList(complete=False)` | `False` | 存在 | ❌ `BLOCKED` | 分页拉取残缺，禁止本地降级 | **C28** |
| `ActivityList([], complete=False)` | `False` | 存在 | ❌ `BLOCKED` | 空列表但拉取异常，禁止假定为空 | **C30** |
| `ActivityList([], complete=True)` | `True` | 存在 (合规) | ✅ `VALID` | 证明无已报课程，允许本地冲突判定 | **C29** |
| `ActivityList([...], complete=True)` | `True` | 存在 (合规) | ✅ `VALID` | 完整已报列表，按时段/周次逐项比对 | C11~C15 |

### 2. 快照新鲜度与上下文一致性判定 (Snapshot Freshness & Consistency)

| 检查维度 | 阈值 / 规则 | 违规后果 | 对应测试 |
| :--- | :--- | :--- | :--- |
| **快照年龄上限** | `elapsed_sec = time.time() - fetched_at > 60.0s` | 判定 `BLOCKED`，拒绝使用过期陈旧数据 | **C31** |
| **时钟回拨/未来时间戳** | `elapsed_sec < -5.0s` | 判定 `BLOCKED`，时钟异常安全拦截 | C31 |
| **会话/运行隔离** | `snapshot.run_id != current_run_id` | 判定 `BLOCKED`，禁止跨批次跨运行盲目复用 | **C32** |
| **快照后本地状态变更** | 存在 StateStore 记录的 `updated_at > fetched_at` | 判定 `BLOCKED`，本地发生抢课变更，快照陈旧须重拉 | **C33** |
| **未决状态阻断** | 存在 `UNKNOWN` / `SUBMITTING` / `VERIFYING` | 判定 `BLOCKED`，优先对账复核，禁止降级决策 | **C38** |
| **可重试状态放行** | 仅存在 `RETRYABLE` 记录，无未决记录 | 允许放行，按本地课表与配额规则评估 | **C37** |

---

## 三、HTTP 状态码与熔断器处理边界

| HTTP 状态码 | 类别 | 是否累加熔断失败计数 | 是否触发本地降级 | 抢课队列决策行为 | 对应测试 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **200 (有效响应)** | 成功 | 清零（NORMAL）；恢复（HALF_OPEN） | 否 | 解析 `data` 布尔值，True 冲突拦截，False 放行 | C01, C05 |
| **200 (数据残缺)** | 响应缺陷 | 计为探测失败（HALF_OPEN 重新 OPEN） | 是 | 安全阻断或切入本地规则降级 | C07 |
| **400 (Bad Request)** | 客户端业务错误 | **否 (不计入基础设施故障)** | 否 | 阻断当前候选活动提交，不影响熔断器健康度 | **C36** |
| **401 (Unauthorized)** | 客户端认证失效 | **否 (不计入基础设施故障)** | **否 (严禁盲降)** | 阻断提交，提示必须先恢复认证，等待 Token 刷新 | **C34** |
| **403 (Forbidden)** | 客户端权限不足 | **否 (不计入基础设施故障)** | 否 | 阻断当前活动提交 | **C35** |
| **500 / 502 / 503** | 基础设施故障 | **是 (累加 failure_count)** | **是 (DEGRADED_LOCAL)** | 达到阈值 (3次) 切入 OPEN，由 LocalConflictChecker 托底 | C02, C03 |
| **-1 (网络超时/连接中断)** | 基础设施故障 | **是 (累加 failure_count)** | **是 (DEGRADED_LOCAL)** | 立即记录基础设施异常，按退避策略调度探测 | C02, C22 |

---

## 四、补充验收测试用例 (C27 ~ C38) 详细报告

测试套件：`tests/test_conflict_fallback.py`

| 用例编号 | 用例名称与验证重点 | 预期结果 | 实测状态 |
| :--- | :--- | :--- | :--- |
| **C27** | `test_C27_items_missing_complete_attribute_blocked`<br>验证普通 `list` 缺少 `complete` 属性时，`check_data_integrity` 严格拒绝自动推断，返回 `BLOCKED` | 阻断且 details 标记 `has_complete_attr=False` | **PASS** |
| **C28** | `test_C28_incomplete_pagination_blocked`<br>验证分页未拉取全量（`complete=False`）时，`check_conflict` 严格判定为 `BLOCKED` | 阻断且 reason 指明分页不完整 | **PASS** |
| **C29** | `test_C29_empty_list_with_complete_true_allows_evaluation`<br>验证完整可信的空报名列表（`complete=True`）允许进入本地规则计算，无冲突时放行 | 判定 `CLEAN`，允许进入后续报名流程 | **PASS** |
| **C30** | `test_C30_empty_list_with_complete_false_blocked`<br>验证空列表但 `complete=False`（如超时返回空结构），禁止假定为空，必须阻断 | 判定 `BLOCKED`，安全屏障拦截 | **PASS** |
| **C31** | `test_C31_expired_local_snapshot_blocked`<br>验证本地数据获取时间超过 `max_snapshot_age_sec`（如 120s > 60s）时，必须阻断 | 判定 `BLOCKED`，指明数据超过有效期 | **PASS** |
| **C32** | `test_C32_mismatched_run_id_blocked`<br>验证本地数据快照的 `run_id` 与当前执行上下文不一致时，严禁跨运行盲目复用 | 判定 `BLOCKED`，指明 run_id 不匹配 | **PASS** |
| **C33** | `test_C33_state_change_after_snapshot_requires_revalidation`<br>验证快照获取后本地 StateStore 发生成功抢课等状态变更时，快照失效必须重新刷新 | 判定 `BLOCKED`，指明本地状态已变更 | **PASS** |
| **C34** | `test_C34_401_unauthorized_blocks_submission_without_local_fallback`<br>验证服务端返回 401 时，禁止本地盲目降级，阻断提交，且熔断器保持 NORMAL | 返回 False，无本地降级事件，failure_count=0 | **PASS** |
| **C35** | `test_C35_403_forbidden_blocks_submission`<br>验证服务端返回 403 权限不足时，明确阻断当前活动提交，熔断器失败计数不累加 | 返回 False，熔断器 NORMAL，failure_count=0 | **PASS** |
| **C36** | `test_C36_400_bad_request_does_not_accumulate_infra_failure`<br>验证连续 5 次 400 参数异常时，严格不累计基础设施故障计数，熔断器绝不切入 OPEN | 5次均阻断，failure_count 严格保持 0 | **PASS** |
| **C37** | `test_C37_only_retryable_allows_local_evaluation`<br>验证 StateStore 仅存在 `RETRYABLE` 记录（对账确认未报名）时，允许进入本地规则评估 | 未决记录为0，本地规则检查通过放行 | **PASS** |
| **C38** | `test_C38_unknown_with_retryable_still_blocked`<br>验证 StateStore 同时存在 `RETRYABLE` 与 `UNKNOWN` 时，`UNKNOWN` 安全屏障起绝对主导，严格阻断 | 判定 `BLOCKED`，指明存在未解决 UNKNOWN | **PASS** |

---

## 五、全量自动化测试汇总

执行命令：`python tests/run_tests.py`（集成 NetworkGuard 全局测试网络隔离屏障）

```text
Ran 402 tests in 29.672s

OK
```

### 测试集分类明细

| 测试套件文件 | 覆盖领域 | 用例数 | 结果 |
| :--- | :--- | :---: | :---: |
| `tests/test_conflict_fallback.py` | Phase 5 checkConflict 智能降级与本地安全检查 (C01~C38) | 38 | **38/38 PASS** |
| `tests/test_audit_logger.py` | 统一审计日志与精准生命周期事件记录 (A01~A15) | 15 | **15/15 PASS** |
| `tests/test_result_reporter.py` | 抢课结果报告与数据一致性对账 (R01~R12) | 12 | **12/12 PASS** |
| `tests/test_report_consistency.py` | 报告数据一致性补充验收 (RC01~RC07) | 7 | **7/7 PASS** |
| `tests/test_multiprocess_concurrency.py`| 多进程文件锁、并发互斥与原子预留 (M01~M13) | 13 | **13/13 PASS** |
| `tests/test_circuit_breaker.py` | 列表请求熔断器、自适应退避与半开探测 (C01~C22) | 22 | **22/22 PASS** |
| `tests/test_unknown_barrier.py` | UNKNOWN 安全屏障、断点自愈与防超报 (U01~U24) | 24 | **24/24 PASS** |
| `tests/test_auth_initializer.py` | 认证自愈初始化器与原子更新 (I01~I14) | 14 | **14/14 PASS** |
| `tests/test_auth_diagnostics.py` | 认证诊断与错误归因矩阵 (D01~D17) | 17 | **17/17 PASS** |
| `tests/test_config_wizard.py` | 配置引导与向导流程 (W01~W21) | 21 | **21/21 PASS** |
| `tests/test_system_doctor.py` | 系统体检与环境诊断 (D01~D18) | 18 | **18/18 PASS** |
| 其他回归套件 (auth, reliability 等) | 编码兼容、课表冲突、网络安全防御等历史基线回归 | 201 | **201/201 PASS** |
| **总计** | **全系统集成与单元测试** | **402** | **402/402 PASS** |

---

## 六、代码变更清单 (Git Diff)

```text
On branch phase5/check-conflict-fallback
modified:   core/conflict_checker.py
modified:   lejian.py
modified:   tests/test_conflict_fallback.py
```

### 1. `core/conflict_checker.py`
- 新增 `ActivityList` 类定义（包含 `complete`, `total`, `fetched`, `error`, `http_code`, `fetched_at`, `run_id`, `source`）。
- `LocalConflictChecker.check_data_integrity`:
  - 强制检查 `hasattr(items, "complete")`，杜绝默认 `True` 推断。
  - 增加 `fetched_at` 存在性、新鲜度（`max_snapshot_age_sec=60.0`）及未来时间戳检测。
  - 增加 `snapshot_run_id` 与 `current_run_id` 一致性比对。
  - 增加 StateStore 状态变更时序探测（`up_dt.timestamp() > fetched_at` 触发重新刷新）。
- `ConflictManager`:
  - 在 `check_activity_conflict` 与 `_execute_local_fallback` 中透传 `current_run_id` 与 `max_snapshot_age_sec`。
  - 细化 401 提示为 `服务端冲突检查返回 401 (Token失效，认证未恢复，禁止报名提交)`，严格禁止自动转入本地盲目降级。
  - 细化 403 提示为 `服务端冲突检查返回 403 (权限不足，禁止报名提交)`。

### 2. `lejian.py`
- 更新 `ActivityList` 定义与 `to_dict()` 导出，补齐 `fetched_at`, `run_id`, `source` 属性。
- `Client.get_activity_list`:
  - 实例化返回 `ActivityList` 时赋予当前 `time.time()` 与会话 `audit_logger.run_id`。

### 3. `tests/test_conflict_fallback.py`
- 导入 `core.conflict_checker.ActivityList` 统一元数据结构。
- 新增 C27 ~ C38 共 12 个针对数据完整性、空列表语义、快照时效、4xx 业务处理及 RETRYABLE/UNKNOWN 兼容性的全量测试用例。

---

## 七、剩余风险与上线建议

1. **真实网络对齐建议**：
   - 本地快照时效上限默认设定为 `60.0s`。在抢课高峰期，如果网络极其拥堵导致一次列表请求耗时较长，60s 的快照时效完全足够支撑单轮抢课候选队列的本地规则比对，同时避免因使用数分钟前的陈旧快照而导致配额误判。
2. **分支封版承诺**：
   - 所有变更已严格限制在 `phase5/check-conflict-fallback` 分支。
   - 未执行与 `main` 分支的合并，未推送远程。
   - 严禁在测试与调试过程中访问任何真实抢课环境接口。

---

## 八、封版结论

Phase 5 的核心设计思想「**允许降级，但绝不允许失去安全保护**」已在代码和测试层面完成彻底闭环。所有数据完整性漏洞、时间跨度漏洞与状态穿透漏洞均被系统化封死。

至此，**Phase 5 正式封版，代码状态冻结，准予推进至 Phase 6。**
