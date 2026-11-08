# 第四轮优化 Phase 2 最终封版报告：原子预留幂等性与事务边界加固

**封版日期**：2026-11-08  
**当前分支**：`phase2/unknown-barrier-hardening`  
**验证状态**：全量 319 项自动化测试 100% 通过（基线 276 项 -> Phase 1 294 项 -> Phase 2 主体 302 项 -> Phase 2 补充 313 项 -> Phase 2 封版 319 项）

---

## 一、本次封版任务概述

根据《Phase 2 最终封版检查：原子预留幂等性》的要求，本轮针对 `reserve_submitting` 的相同 `activityId` 重复预留防护、SQLite 事务边界完整性（杜绝内部嵌套 commit）以及 `cmd_grab` 入口级单实例锁有效性进行了精准检查与硬化验证。

---

## 二、任务一：相同 activityId 重复预留检查与修复

### 1. 历史漏洞成因分析

在原先 SQL 检查中：
```sql
SELECT * FROM registration_records
WHERE status IN (?, ?, ?) AND activity_id != ?
LIMIT 1;
```
由于显式排除了 `activity_id != ?`，导致：
- 若当前活动自身已经处于 `SUBMITTING`、`UNKNOWN` 或 `VERIFYING` 状态，该查询会忽略当前活动；
- 导致同一活动在未完成服务端对账或正在提交的情况下，被再次预留并进入 `SUBMITTING`，甚至修改了 `attempts` 计数；
- 若当前活动已处于 `SUCCESS` 状态，亦可能被重复预留并覆盖为 `SUBMITTING`。

### 2. 状态机硬化判定矩阵

在 [`core/state_store.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/core/state_store.py#L136-L230) 的 `reserve_submitting` 内部新增当前活动自身状态的排他判定：

| 当前活动自身状态 | 是否允许原子预留为 SUBMITTING | 处理动作与返回信息 |
| :--- | :---: | :--- |
| **`SUBMITTING`** | **严格禁止** | 事务回滚，返回 False，提示 `当前活动已处于 SUBMITTING 状态，禁止重复预留` |
| **`UNKNOWN`** | **严格禁止** | 事务回滚，返回 False，提示 `当前活动已处于 UNKNOWN 状态，必须先复核，禁止直接预留` |
| **`VERIFYING`** | **严格禁止** | 事务回滚，返回 False，提示 `当前活动已处于 VERIFYING 状态，禁止并发预留` |
| **`SUCCESS`** | **严格禁止** | 事务回滚，返回 False，提示 `当前活动已报名成功 (SUCCESS)，禁止重复预留` |
| **`RETRYABLE`** | **允许预留** | 经复核确认未报且业务规则二次校验通过后，允许原子进入 `SUBMITTING` |
| **不存在任何记录** | **允许预留** | 新候选活动首次预留，允许原子进入 `SUBMITTING` |
| **其他活动存在未决记录** | **严格禁止** | 全局安全屏障互斥锁定，事务回滚，返回 False |

所有检查与写入完全被包裹在同一个 `BEGIN IMMEDIATE;` 事务锁内，杜绝 TOCTOU 竞态。

---

## 三、任务二：SQLite 事务边界与原子性检查

### 1. 事务边界审查结论

| 检查维度 | 审查结论 | 技术事实与证据 |
| :--- | :---: | :--- |
| **是否使用相同 connection** | **是** | 整个 `reserve_submitting` 从 `conn = self._get_conn()` 开始，全链路复用同一连接 |
| **是否存在内部 commit** | **否** | 修复了原先调用 `self.record_submitting` 导致 `with conn:` 触发内部提前 commit 的缺陷，所有 DML 在当前事务中平铺执行，统一在最后执行 `conn.execute("COMMIT;")` |
| **是否存在独立 connection** | **否** | 未打开任何独立连接，不产生连接池死锁或未提交读脏数据 |
| **异常后是否完整 rollback** | **是** | 任何阶段抛出异常（包括底层写盘错误），均触发 `conn.execute("ROLLBACK;")`，数据库回到事务开始前的状态 |

### 2. 回滚验证测试（M12）
在 `test_M12` 中，对处于 `RETRYABLE` 状态的活动模拟预留过程中的写盘中断异常。验证发现异常抛出后，数据库完整回滚，使用全新独立连接重新查询时，记录依然严格为 `RETRYABLE`，`attempts` 保持原值，证明了真正的事务原子性。

---

## 四、任务三：真实入口锁（InstanceLock）集成检查

### 1. `cmd_grab()` 执行时序审查

位于 [`lejian.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/lejian.py#L728-L744)：
```python
def cmd_grab():
    cfg = load_json(CONFIG, {})
    st = load_json(STATE)
    if not st:
        sys.exit("没有 state.json，请先登录并跑 tools/make_state.py 或 tools/resync_from_app.py")

    os.makedirs(DATA_DIR, exist_ok=True)
    lock = InstanceLock(LOCK_PATH)
    if not lock.acquire():
        log("[错误] 已有抢课进程正在运行，拒绝启动第二个抢课实例！")
        return

    try:
        _cmd_grab_inner(cfg, st)
    finally:
        lock.release()
```

- **锁获取时机**：在 `c.ensure_token()`、`c.activities()`、课表解析与任何业务逻辑之前获取；
- **锁失败语义**：获取失败打印错误并 `return`，完全不调用 `_cmd_grab_inner`；
- **生命周期覆盖**：`try ... finally: lock.release()` 完整覆盖抢课主循环的全生命周期；
- **异常析构保障**：无论正常退出、业务抛错还是硬崩溃（`os._exit`），操作系统自动回收文件锁。

### 2. 真实入口测试证据（M13）
在 `test_M13` 中，主进程持有锁，创建独立子进程调用真实 `lejian.cmd_grab()`。子进程在获取锁失败后直接拦截退出，子进程发出的 HTTP 请求次数严格为 0。

---

## 五、新增测试用例运行结果（M08～M13）

测试文件：[`tests/test_multiprocess_concurrency.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/tests/test_multiprocess_concurrency.py)

```powershell
python -m unittest -v tests.test_multiprocess_concurrency
```

| 用例 ID | 测试方法名 | 场景与断言重点 | 测试结果 |
| :---: | :--- | :--- | :---: |
| **M08** | `test_M08_same_activity_duplicate_reservation_rejected` | 相同 activityId 连续调用两次 `reserve_submitting`，第二次在事务内被拒绝并提示 SUBMITTING 冲突 | ✅ PASS (0.01s) |
| **M09** | `test_M09_unknown_activity_reservation_rejected` | 当前活动为 `UNKNOWN`，再次尝试预留必须拒绝，必须先通过复核通道 | ✅ PASS (0.01s) |
| **M10** | `test_M10_retryable_activity_allowed_after_rule_revalidation` | 当前活动为 `RETRYABLE`，经二次规则校验通过后允许原子预留为 SUBMITTING 并提交 | ✅ PASS (0.02s) |
| **M11** | `test_M11_success_activity_reservation_rejected` | 当前活动为 `SUCCESS`，拒绝再次预留，杜绝重复报名 | ✅ PASS (0.01s) |
| **M12** | `test_M12_sqlite_transaction_boundaries_and_rollback_atomicity` | SQLite 事务边界检验：模拟写入异常，完整回滚且无中间提交，状态完全保全无污染 | ✅ PASS (0.02s) |
| **M13** | `test_M13_entry_level_cmd_grab_second_process_zero_http_calls` | 入口级单实例锁验证：主进程持有锁时，第二独立进程执行 `cmd_grab()` 被拦截，HTTP 调用为 0 | ✅ PASS (0.68s) |

---

## 六、全量测试套件执行结果

```powershell
python -m tests.run_tests
```

**执行结果**：
- **测试总数**：319 项
- **通过数量**：319 项
- **失败数量**：0 项
- **错误数量**：0 项
- **总耗时**：29.142s
- **测试状态**：**OK**

---

## 七、Git 变更清单

```
modified:   core/state_store.py
modified:   tests/test_multiprocess_concurrency.py
untracked:  docs/17-第四轮优化Phase2最终封版报告.md
```

至此，Phase 2 核心业务加固与补充并发防护已全部通过验证，正式封版。
