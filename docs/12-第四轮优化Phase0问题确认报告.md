# 乐健体育抢课系统第四轮优化 Phase 0 问题确认与基线摸底报告

> **目标仓库**：`vindcation0/lejianPE`  
> **当前分支**：`main`（Commit: `d91761e`）  
> **基线测试**：276 项测试全部通过（100% PASS，耗时 19.38s）  
> **报告日期**：2026-11-08  
> **执行依据**：《乐健体育抢课系统第四轮优化执行方案.md》Phase 0

---

## 一、Git 工作区与基线状态核验

1. **分支状态**：
   - 当前处于 `main` 分支，与 `origin/main` 完全同步；
   - 最新提交：`d91761e docs: 2026-11-08 抢课记录（2节零超额）`。
2. **工作区状态**：
   - 无任何脏代码变更；
   - 存在 2 个未追踪文件：
     - `docs/乐健体育抢课系统第四轮优化执行方案.md`（优化方案源文件）
     - `tools/_probe_now.py`（用户创建的单次延迟探测脚本）
3. **配置文件现状 (`config.json`)**：
   - `check_conflict: false`（因旧逻辑在接口异常时硬性阻断抢课，生产环境当前被迫手动关闭）；
   - `current_week: 10`；
   - `watch_from: "13:59:00"`, `watch_until: "16:00:00"`；
   - 尚无 `conflict_check_mode` 及预热 `preflight_interval_seconds` 相关配置字段。
4. **自动化测试基线**：
   - 执行 `python -m tests.run_tests`，涵盖现有全部 13 个测试套件；
   - **结果：Ran 276 tests in 19.385s, OK (100% PASS)**。

---

## 二、第四轮优化方案指出问题的代码级逐项核实

经仔细比对目标分支最新代码，确认方案指出的核心可靠性缺陷与统计口径问题**全部客观存在**，且定位到了精确的代码行号。

### 1. 【P0 级】活动列表请求失败与熔断器集成失效（缺陷严重）

- **涉及文件**：`lejian.py:819-840`, `lejian.py:284-323`, `core/circuit_breaker.py:106-124`
- **代码核实事实**：
  1. `Client.activities()` 内部在遭遇网络超时或 HTTP 500/502 时，内部重试 3 次后通过 `self._note_fail(code)` 记录，并**返回 `ActivityList(..., complete=False, ...)`，绝不向上抛出异常**；
  2. 在 `lejian.py` 轮询主循环中：
     ```python
     try:
         items = c.activities(page_size=200, req_timeout=6)
     except Exception as e:
         # 此块绝不会被执行到！
         circuit_breaker.record_failure(...)
         reporter.record_http_failure(...)
     ```
  3. 随后主循环执行到完整性判断：
     ```python
     if not getattr(items, "complete", True):
         # 调用 record_success(is_complete=False)
         circuit_breaker.record_success(is_complete=False)
     ```
  4. 深入查看 `core/circuit_breaker.py:119-120`：
     ```python
     def record_success(self, is_complete: bool = True) -> str:
         if self.state == CircuitState.HALF_OPEN:
             ...
         elif self.state == CircuitState.CLOSED:
             self.failure_count = 0  # ！！！致命缺陷！！！
     ```
  5. **缺陷影响**：在熔断器正常 `CLOSED` 运行期间，当服务端出现连续 HTTP 500、网关 502 或超时时，`activities()` 返回 `complete=False`，主循环调用 `record_success(is_complete=False)`，熔断器在 CLOSED 下直接将 `self.failure_count = 0` 清零！
  6. **结论**：**真实服务端发生持续故障时，熔断器不仅不会累加失败计数，反而会被不断清零，永远无法触发 OPEN 熔断保护！**

---

### 2. 【P0 级】UNKNOWN 安全屏障与熔断器独立性、复核严密性

- **涉及文件**：`lejian.py:846-880`, `core/registration_manager.py:206-234`, `core/request_manager.py:236-267`
- **代码核实事实**：
  1. 当前主循环已包含 `state_store.get_unresolved_records()` 优先复核逻辑；
  2. 但熔断器与 UNKNOWN 的边界需要严格保证：**熔断器恢复至 CLOSED 仅代表底层网络/接口具备可用性，绝对不能解除 UNKNOWN 屏障**；
  3. `verify_registration` 在未检索到目标活动时返回 `(False, None, None)`，保持 UNKNOWN；但在列表分页异常、残缺或结构突变时，需要建立更具鲁棒性的端到端防御测试，确保崩溃恢复与配额重算闭环。

---

### 3. 【P1 级】请求审计日志与提交次数统计口径混淆

- **涉及文件**：`lejian.py:856, 872, 981`, `core/result_reporter.py:49-66`
- **代码核实事实**：
  1. `core/result_reporter.py:51` 中：
     ```python
     def record_attempt(self, activity_id: str, activity_name: str, status: str, reason: str = ""):
         self.attempts_total += 1
         ...
     ```
  2. 在 `lejian.py` 中：
     - 行 981：实际抢课时调用 `reporter.record_attempt(aid, name, final_status, reason)`；
     - 行 856：UNKNOWN 经只读复核确认成功时调用 `reporter.record_attempt(unres_aid, unres_name, "SUCCESS", "复核确认成功")`；
     - 行 872：UNKNOWN 经只读复核确认未报时调用 `reporter.record_attempt(unres_aid, unres_name, "FAILED", "复核确认未报名")`；
  3. **缺陷影响**：如果某个活动由于网络超时产生了 1 次 signUp 提交（记录为 UNKNOWN，`attempts_total=1`），后续轮询中执行只读复核确认成功，又调用一次 `record_attempt`（`attempts_total` 变成 2）。**复核操作被错误地累加成了新的提交尝试！**
  4. **结论**：候选规则评估（Candidate）、实际网络提交（Submission）、只读复核（Verification）与最终对账（Reconciliation）必须彻底解耦。

---

### 4. 【P1 级】HTTP 故障计数与故障持续时间统计失真

- **涉及文件**：`lejian.py:251-283, 822-831`, `core/result_reporter.py:68-71`
- **代码核实事实**：
  1. 类似问题 1，`reporter.record_http_failure(http_code)` 处于未被触发的 `except Exception` 块中，真实列表请求抛出的 500/超时完全没有进入 `ResultReporter` 的统计器；
  2. `Client._note_fail` 方法：
     ```python
     if getattr(self, "_last_fail", None) != code:
         self._last_fail = code
     ```
     该逻辑对相同状态码进行了去重，10 次连续 500 只输出并记录 1 条日志，导致事件记录无法等同于 HTTP 请求失败次数；
  3. `_note_fail` 与 `_note_ok` 直接向 `logs/events-YYYYMMDD.jsonl` 写文件，绕过了统一的 `AuditLogger` 体系，存在重复实现与格式不统一。

---

### 5. 【P1 级】最终对账失败语义错误覆盖为零报名（高风险）

- **涉及文件**：`lejian.py:1021-1022`, `core/result_reporter.py:72-104`
- **代码核实事实**：
  1. `lejian.py:1021-1022` 逻辑：
     ```python
     reporter.set_reconciliation_result(
         reconciled_items=report.get("items", []) if report.get("success") else [],
         ...
     ```
  2. 当全局对账接口发生网络异常或 500 时，`report.get("success")` 为 `False`；
  3. 此时传给 `reconciled_items` 的是一个**空列表 `[]`**；
  4. `ResultReporter.set_reconciliation_result` 接收到空列表后，直接计算出：
     `self.total_server_registered = len(reconciled_items) = 0`
     `self.this_run_success = 0`
  5. **缺陷影响**：当抢课结束执行最终对账遇到服务端不可用时，系统向控制台和 Markdown 报告输出“服务端确认报名数量为 0”，将**“对账失败/状态未决”**篡改为了**“确认未成功”**，给用户造成已抢名额丢失或根本没抢到的严重误判！

---

### 6. 【P2 级】服务端冲突检查机制僵化、缺乏安全自动降级

- **涉及文件**：`lejian.py:966-975`, `config.json`
- **代码核实事实**：
  1. `lejian.py` 当前逻辑：
     ```python
     if cfg.get("check_conflict", True):
         try:
             conf = c.check_conflict(act_item.get("id"))
             if conf is True:
                 return False, "服务端判定冲突"
             elif conf is None:
                 return False, "服务端冲突检查接口异常（结果未知，安全阻断）"
     ```
  2. 仅支持简单的布尔开启/关闭。如果开启时服务端冲突接口瘫痪（返回 None），系统硬性阻断所有提交，导致即使本地课表与配额完全安全可用，也无法抢课；
  3. 用户因此在生产环境中被迫直接在 `config.json` 中配置 `"check_conflict": false`，彻底放弃了冲突校验保护；
  4. **结论**：需要支持 `strict`（严格）、`local`（仅本地）、`auto`（优先服务端，满足 7 项安全条件时平滑降级至本地）三态模式。

---

### 7. 【P2 级】缺乏开抢前低频健康预热机制

- **涉及文件**：`lejian.py:783-790`
- **代码核实事实**：
  1. 在 `watch_from` 到达前，主循环完全处于 `time.sleep` 空转状态；
  2. 开抢前数分钟无法提前感知 Token 是否过期、列表接口是否已有 500、学校排课是否已变更；
  3. 一旦 13:59:00 正式开抢，由于没有预热，首次请求可能同时承担 Token 换发、网络探测与抢课竞争，极易导致开局阻塞。

---

## 三、Phase 0 问题确认清单汇总表

| 序号 | 方案任务 | 确认状态 | 当前缺陷核心定位 | 解决策略要点 |
| :---: | :--- | :---: | :--- | :--- |
| **1** | **任务一：列表请求与熔断器集成** | **【严重缺陷，100% 确认】** | `activities()` 失败不抛异常返回 `complete=False`；`record_success(False)` 在 CLOSED 状态直接把 `failure_count` 清零，导致永远无法触发熔断。 | 列表请求失败直接记录基础设施故障；修改 `record_success` 语义，残缺列表绝不清零失败计数；连续故障达到阈值强制进入 OPEN。 |
| **2** | **任务二：强化 UNKNOWN 安全屏障** | **【机制需加强，100% 确认】** | 熔断器恢复可能被误当作业务状态恢复；复核需严格确保“完整列表 + 明确存在且 `isRegister=False`”才可判定未报。 | 确保熔断器状态转换与 UNKNOWN 状态严格解耦；补充针对网络波动、接口残缺、分页丢失时的安全屏障测试。 |
| **3** | **任务三：重构请求审计日志** | **【口径混淆，100% 确认】** | `record_attempt` 既计入首次提交又计入复核，导致复核操作虚增提交总数；规则拦截未形成独立事件。 | 统计拆分为 Candidate（候选评估）、Submission（实际 signUp）、Verification（只读复核）、Reconciliation（对账）。 |
| **4** | **任务四：修复故障统计与持续时间** | **【统计失真，100% 确认】** | `record_http_failure` 处于未执行的 catch 块；`_note_fail` 状态码去重且脱离 `AuditLogger`。 | 列表单次 HTTP 失败全量上报统计；统一通过 `AuditLogger` 记录，分离请求失败次数与故障状态时段。 |
| **5** | **任务五：修复对账失败语义** | **【高风险误报，100% 确认】** | `reconciled_items` 在 `success=False` 时传入 `[]`，报告被误判为“服务端确认报名为 0”。 | 引入 `SUCCESS`、`FAILED`、`UNVERIFIED` 三态；对账失败绝不允许输出零报名结论，必须显式标红告警。 |
| **6** | **任务七：冲突检查三态降级** | **【模式僵化，100% 确认】** | 仅布尔开/关；服务端接口故障时硬性阻断抢课，导致用户生产环境被迫全局关掉冲突检查。 | 引入 `strict` / `local` / `auto` 三态模式；在满足 7 项前置安全条件时，auto 模式自动平滑降级到本地规则。 |
| **7** | **任务八：低频健康预热机制** | **【功能缺失，100% 确认】** | `watch_from` 前纯 sleep，无健康度预警与 Token 预备。 | 增加低频（如 15~30s）只读探测阶段；绝不调用 signUp；提前感知异常并使熔断器就绪。 |

---

## 四、下一步建议

Phase 0 基线检查与代码缺陷排查已全部完成。  
建议严格遵循既定规划：**进入 Phase 1（实施任务一：修复活动列表请求与熔断器的集成）**。每完成一个 Phase 均跑通全量测试并核验 Git Diff，绝不跨阶段实施。
