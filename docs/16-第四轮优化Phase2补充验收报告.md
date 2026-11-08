# 第四轮优化 Phase 2 补充验收报告：多进程并发保护与状态原子性

**验收日期**：2026-11-08  
**当前分支**：`phase2/unknown-barrier-hardening`  
**验证状态**：全量 313 项自动化测试 100% 通过（基线 276 项 -> Phase 1 294 项 -> Phase 2 主体 302 项 -> Phase 2 补充 313 项）

---

## 一、验收工作概述

根据《Phase 2 补充任务：多进程并发保护与状态原子性验收》指导规范，本次补充验收聚焦于**多进程操作系统级互斥**与**SQLite 状态持久化原子性**，不破坏已有 UNKNOWN 状态机设计，不修改无关业务逻辑，全量保留既有测试用例，并补齐了真实多进程测试与状态转换边界测试。

### 核心验收成效

1. **操作系统级单实例互斥（Task 1）**：
   - 审查确认 `core/instance_lock.py` 实现基于 Windows `msvcrt.locking(LK_NBLCK, 1)` 与 POSIX `fcntl.flock`。
   - 文件锁生命周期绑定至操作系统内核进程句柄，在进程崩溃（`os._exit`）、强杀或断电时由操作系统自动回收，无需依赖脆弱的固定 PID 文件。
   - 第二个进程启动时明确被拦截拒止，绝不进入登录认证与抢课循环。

2. **SQLite 状态原子预留（Task 2）**：
   - 修复了原先 `has_unresolved()` 检查与 `record_submitting()` 写入之间的 Check-Then-Act（TOCTOU）并发竞态窗口。
   - 在 `StateStore` 中实现 `reserve_submitting(aid, name)`，采用 SQLite `BEGIN IMMEDIATE;` 事务锁，在单次原子操作中完成“检查全局未决状态”与“创建/更新 SUBMITTING 记录”。
   - 保证若有任何其他活动处于 `SUBMITTING / UNKNOWN / VERIFYING` 状态，立即回滚并拒绝，杜绝两个独立进程同时将不同活动置为可提交状态。

3. **真实多进程并发测试套件（Task 3：M01～M07）**：
   - 全面采用 Python `multiprocessing.Process`（兼容 Windows `spawn` 进程创建模式）创建真实独立的操作系统进程，彻底杜绝以线程模拟进程的虚假测试。
   - 7 项真实并发场景全部通过验证。

4. **UNKNOWN / RETRYABLE 状态转换与配额闭环（Task 4：U21～U24）**：
   - 补充 4 项状态转换边界测试，针对 RETRYABLE 重新校验规则、RETRYABLE 零配额消耗、SUCCESS 课表防重统计、持久化写入失败安全阻断进行了端到端验证。

---

## 二、任务一：单实例运行保护机制深度审查

### 1. 机制架构与平台实现

位于 [`core/instance_lock.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/core/instance_lock.py) 与 [`lejian.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/lejian.py#L734-L744)：

```
                        进程 A 启动 (PID 1000)
                                │
                                ▼
                       打开锁文件 lejian.lock
                                │
                                ▼
                 Windows: msvcrt.locking(fd, LK_NBLCK, 1)
                 POSIX:   fcntl.flock(fd, LOCK_EX | LOCK_NB)
                                │
                 ┌──────────────┴──────────────┐
                 │ 成功                        │ 被占用 (进程 B 启动 PID 2000)
                 ▼                             ▼
           获得系统级文件锁              抛出 BlockingIOError / OSError
                 │                             │
                 ▼                             ▼
          执行 _cmd_grab_inner           记录日志 [错误] 已有抢课进程正在运行...
                 │                             │
                 ▼                             ▼
          退出/崩溃: 内核自动回收         立即退出，零网络调用，零抢课执行
```

### 2. 核心审查指标确认

| 检查项 | 审查结论 | 技术实现与证据 |
| :--- | :---: | :--- |
| **1. 两个独立 Python 进程能否同时启动** | **否** | 通过操作系统文件锁互斥，第二进程在启动初期即被拦截（用例 M01、M02） |
| **2. 两个进程能否同时获得报名执行权** | **否** | `InstanceLock.acquire()` 具有排他独占语义，仅先到者可返回 True |
| **3. 是否存在进程内锁代替系统级锁** | **否** | 使用 `msvcrt`（Windows 操作系统 API）与 `fcntl`（POSIX 系统调用），非 Python `threading.Lock` |
| **4. 进程异常退出后锁能否自动释放** | **是** | 锁绑定在 OS 打开文件句柄上，进程异常终止（`os._exit`）后由 OS 内核自动析构回收（用例 M03） |
| **5. 第二进程能否明确感知冲突** | **是** | `acquire()` 返回 `False`，`cmd_grab()` 明确打印 `已有抢课进程正在运行，拒绝启动第二个抢课实例！` 并安全退出 |

---

## 三、任务二：SQLite 状态原子性与 TOCTOU 防御

### 1. 竞态风险分析

修改前，若绕过外层进程锁或因极端边界导致多进程并发执行：
```
进程 A: has_unresolved() -> False
进程 B: has_unresolved() -> False
进程 A: record_submitting("1001") -> 写入 SUBMITTING
进程 B: record_submitting("1002") -> 覆盖/写入 SUBMITTING
进程 A: submit_once("1001")
进程 B: submit_once("1002")  <-- 产生了双重并发提交，击穿配额！
```

### 2. `reserve_submitting` 原子事务设计

位于 [`core/state_store.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/core/state_store.py#L136-L173)：

```python
def reserve_submitting(self, activity_id: str, activity_name: str) -> Tuple[bool, Optional[Dict], str]:
    conn = self._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        try:
            # 1. 检查是否存在其他活动的未决记录
            cur = conn.execute(
                "SELECT * FROM registration_records WHERE status IN (?, ?, ?) AND activity_id != ? LIMIT 1",
                (RegistrationStatus.SUBMITTING, RegistrationStatus.UNKNOWN, RegistrationStatus.VERIFYING, str(activity_id))
            )
            other_row = cur.fetchone()
            if other_row:
                conn.execute("ROLLBACK;")
                other_rec = dict(other_row)
                return False, other_rec, f"已存在未决活动 {other_rec.get('activity_name')} (id={other_rec.get('activity_id')}, 状态={other_rec.get('status')})，全局互斥锁定"

            # 2. 写入/更新当前记录（调用 self.record_submitting 保证兼容单测与主逻辑一致性）
            cur_rec = self.record_submitting(activity_id, activity_name)
            return True, cur_rec, "OK"
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
    except StateStoreError:
        raise
    except Exception as e:
        raise StateStoreError(f"原子预留 SUBMITTING 失败 {activity_id}: {e}") from e
```

### 3. 防御效果
- **排他写锁**：`BEGIN IMMEDIATE` 在事务开启瞬间即向 SQLite 请求保留锁（RESERVED LOCK），任何其他并发写进程必须等待或被阻断。
- **原子性判定**：检查其他未决记录与写入新记录完全位于同一事务内部。若存在其他未决记录，立即回滚并返回 `(False, other_rec, ...)`，不会修改数据库。
- **向下兼容**：保留并内联复用 `self.record_submitting`，确保测试用例对底层写入的模拟（如 `test_T13` 磁盘 I/O 错误模拟）无缝透传，保证原有 302 项测试 100% 兼容。

---

## 四、任务三：真实多进程测试套件（M01～M07）

测试文件：[`tests/test_multiprocess_concurrency.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/tests/test_multiprocess_concurrency.py)  
实现原则：严禁线程模拟，统一使用 `multiprocessing.Process`，顶层可序列化 Worker 函数，支持 Windows `spawn` 模式。

| 用例 ID | 测试名称 | 进程操作与验证重点 | 运行耗时 | 测试结果 |
| :---: | :--- | :--- | :---: | :---: |
| **M01** | `test_M01_two_processes_start_simultaneously` | 两个独立 Python 进程同时抢锁，`multiprocessing.Queue` 收集结果，严格断言 `[True, False]` | 0.62s | ✅ PASS |
| **M02** | `test_M02_process_b_starts_while_process_a_holds_lock` | 进程 A 持有锁期间进程 B 启动，验证 B 被拒、PID 隔离、A 锁持续有效不受干扰 | 0.53s | ✅ PASS |
| **M03** | `test_M03_process_a_abrupt_exit_releases_os_lock` | 进程 A 获锁后通过 `os._exit(42)` 模拟进程突然崩溃被杀，验证系统级文件锁由 OS 内核自动释放，随后进程 B 能成功抢锁 | 0.65s | ✅ PASS |
| **M04** | `test_M04_concurrent_submitting_reservations_are_strictly_atomic` | 两个独立进程同时对不同活动调用 `reserve_submitting`，验证 SQLite 原子事务互斥，严格断言一个成功一个失败，表中处于 `SUBMITTING` 的记录恰为 1 | 0.58s | ✅ PASS |
| **M05** | `test_M05_process_a_unknown_blocks_process_b` | 进程 A 遗留 UNKNOWN 状态后退出，进程 B 启动尝试提交新候选，验证安全屏障阻断 B 且 B 发出的底层 signUp HTTP 请求严格为 0 | 0.54s | ✅ PASS |
| **M06** | `test_M06_process_a_crashes_during_submitting_process_b_recovers_without_resubmit` | 进程 A 在 SUBMITTING 写入后崩溃，进程 B 启动运行对账自愈，验证 B 只发起读复核并转为 SUCCESS，绝不重复发送 signUp（调用数为 0） | 0.55s | ✅ PASS |
| **M07** | `test_M07_concurrent_attempts_after_weekly_limit_never_exceeds_quota` | 当周配额满时两进程并发尝试提交，验证规则拦截器生效，两进程均未发起网络请求，SUCCESS 总数维持为 1 | 0.56s | ✅ PASS |

---

## 五、任务四：UNKNOWN 状态转换与配额防重边界验证（U21～U24）

测试文件：[`tests/test_unknown_barrier.py`](file:///c:/Users/vindacation/Documents/deepseek-harness/lejianPE/tests/test_unknown_barrier.py)

### 1. 状态判断矩阵清晰区分

| 场景 | 服务端返回表现 | 判定结果 | 处理动作与状态跃迁 |
| :--- | :--- | :---: | :--- |
| **复核确认未报名** | `http_code == 200` 且 `complete == True` 且目标在列表中且 `isRegister == False` | **明确未报** | 置为 `RETRYABLE`，解除队列冻结，释放名额 |
| **复核请求失败** | 遇到 HTTP 500/502/503 或网络连接超时 | **未知** | 严格维持 `UNKNOWN`，继续冻结候选队列 |
| **列表残缺截断** | 分页拉取不全返回 `complete == False` | **未知** | 严格维持 `UNKNOWN`，绝不误判为未报名 |
| **服务端未返回目标活动** | 列表中未检索到目标 `activity_id` | **未知** | 严格维持 `UNKNOWN`，绝不误判为未报名 |
| **数据格式异常** | 列表为空或 JSON 解析异常 | **未知** | 严格维持 `UNKNOWN`，绝不轻率判定 |

### 2. 补充用例清单（U21～U24）

| 用例 ID | 测试名称 | 验证的核心契约与场景 | 测试结果 |
| :---: | :--- | :--- | :---: |
| **U21** | `test_U21_retryable_resubmission_must_revalidate_rules` | `RETRYABLE` 状态活动再次尝试提交时，必须重新执行全部业务规则校验（时段、配额、冲突等），校验不通过直接拒绝，零网络请求 | ✅ PASS |
| **U22** | `test_U22_retryable_status_does_not_consume_quota` | 复核为 `RETRYABLE`（未报名）时，无论处理多少次都绝不增加已报配额（`week_done` / `DAY_DONE` 保持不变） | ✅ PASS |
| **U23** | `test_U23_success_already_in_items_does_not_double_count_quota` | UNKNOWN 复核为 SUCCESS 时，若当轮轮询获取的 `items` 已经包含了该活动且标记了已报，触发 `already_in_items` 防重机制，禁止重复累加配额 | ✅ PASS |
| **U24** | `test_U24_state_store_write_failure_aborts_submission_without_network_call` | 状态库发生写入异常（`StateStoreError`）时，立即中止流程，返回 FAILED，绝对不可发起无状态保护的底层 signUp 网络请求 | ✅ PASS |

---

## 六、全量测试执行与基线演进对比

### 1. 本次执行命令与输出

```powershell
# 1. 真实多进程并发专项（7 项全部通过）
python -m unittest tests.test_multiprocess_concurrency
# 输出：Ran 7 tests in 4.637s -> OK

# 2. UNKNOWN 安全屏障专项（24 项全部通过）
python -m unittest tests.test_unknown_barrier
# 输出：Ran 24 tests in 0.299s -> OK

# 3. 全量自动化回归套件（313 项全部通过）
python -m tests.run_tests
# 输出：Ran 313 tests in 26.890s -> OK
```

### 2. 历次优化测试用例演进矩阵

| 阶段 / 里程碑 | 测试用例总数 | 通过率 | 覆盖的核心模块与机制 |
| :--- | :---: | :---: | :--- |
| **Phase 0 基线** | 276 | 100% | 密码登录、Token 轮转、课表解析、基础抢课流程 |
| **Phase 1** | 294 | 100% | 活动列表请求失败分类、熔断器状态机（C01～C18） |
| **Phase 2 主体** | 302 | 100% | UNKNOWN 屏障加固、熔断器解耦、列表完整性校验、崩溃自愈（U01～U20） |
| **Phase 2 补充（当前）** | **313** | **100%** | **系统级单实例文件锁、SQLite 状态原子预留、真实多进程并发（M01～M07）、RETRYABLE 规则复核与配额闭环（U21～U24）** |

---

## 七、Git 变更总结

### 1. 修改文件清单

```
modified:   core/registration_manager.py
modified:   core/state_store.py
modified:   tests/test_unknown_barrier.py
untracked:  tests/test_multiprocess_concurrency.py
untracked:  docs/16-第四轮优化Phase2补充验收报告.md
```

### 2. 差异摘要（Git Diff Summary）
- `core/registration_manager.py`：在步骤 3 调用 `state_store.reserve_submitting(aid, name)`，杜绝 Check-Then-Act TOCTOU 竞态。
- `core/state_store.py`：实现 `reserve_submitting`，利用 `BEGIN IMMEDIATE;` 保证未决检查与 SUBMITTING 写入的强原子性，并无缝复用 `self.record_submitting`。
- `tests/test_unknown_barrier.py`：新增 U21～U24 单元测试。
- `tests/test_multiprocess_concurrency.py`：新增 M01～M07 真实多进程测试套件。
- `docs/16-第四轮优化Phase2补充验收报告.md`：本报告。
