"""
抢课结果统计与报告生成模块 (ResultReporter)
===========================================
第四轮优化 Phase 4 重构：
1. 统一报告数据来源：
   - 运行过程指标来自统一审计日志 AuditLogReader.replay()
   - 最终报名状态来自服务端真实 Reconciliation
   - 本地持久化状态来自 StateStore
2. 彻底分离 Submission 与 Verification，杜绝口径偏差
3. 严格解耦 HTTP 请求失败次数与熔断触发次数
4. 结构化多段式报告（八大部分）：
   一、运行信息
   二、候选评估
   三、真实报名提交
   四、UNKNOWN 复核
   五、服务端最终对账
   六、HTTP 请求与服务器状态
   七、安全状态
   八、最终结论 (SUCCESS / PARTIAL_SUCCESS / FAILED / UNVERIFIED)
5. 控制台与 Markdown 报告数据源 100% 同源一致
6. 全面保持既有接口向后兼容
"""
import collections
import datetime
import os
from typing import Any, Dict, List, Optional, Set, Tuple


class ReportStatus:
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    UNVERIFIED = "UNVERIFIED"


class ResultReporter:
    """抢课结果统计器与统一报告生成器"""

    def __init__(self, run_date: Optional[datetime.date] = None, run_id: Optional[str] = None):
        self.run_date = run_date or datetime.date.today()
        self.run_id = run_id
        self.started_at = datetime.datetime.now()
        self.finished_at: Optional[datetime.datetime] = None
        self.current_week = 0
        self.target_week = 0

        # ---------------- 审计事件回放数据 (Audit Summary) ----------------
        self.audit_summary: Optional[Dict[str, Any]] = None
        self.audit_bound = False
        self.write_error_count = 0
        self.audit_degraded = False

        # ---------------- A. 候选评估 ----------------
        self.candidate_count = 0
        self.candidate_accepted = 0
        self.candidate_rejected = 0
        self.candidate_blocked = 0
        self.candidate_reasons: Dict[str, int] = {}

        # ---------------- B. 真实报名提交 (Submission) ----------------
        self.attempts_total = 0
        self.explicit_success = 0
        self.explicit_failed = 0
        self.unknown_count = 0
        self.attempt_records: List[Dict] = []

        # ---------------- C. UNKNOWN 复核 (Verification) ----------------
        self.verification_count = 0
        self.verification_confirmed_registered = 0
        self.verification_confirmed_unregistered = 0
        self.verification_remains_unknown = 0
        self.verification_avg_duration_ms = 0.0

        # ---------------- D. 服务端最终对账 (Reconciliation) ----------------
        self.reconciliation_performed = False
        self.reconciliation_success = False
        self.reconciliation_status = "UNVERIFIED"  # SUCCESS, FAILED, UNVERIFIED
        self.reconciliation_error: Optional[str] = None
        self.reconciled_items: List[Dict] = []
        self.reconciled_success = 0
        self.reconciled_failed = 0
        self.unresolved_count = 0
        self.historical_count = 0
        self.total_server_registered = 0
        self.by_week_distribution: Dict[str, Any] = {}
        self.by_day_distribution: Dict[str, Any] = {}

        # ---------------- E. 配额与限制 ----------------
        self.normal_registered = 0
        self.max_normal = 2
        self.special_registered = 0
        self.max_per_day = 1
        self.daily_limit_exceeded = False
        self.weekly_limit_exceeded = False

        # ---------------- F. 网络与故障统计 (Infrastructure) ----------------
        self.http_requests_total = 0
        self.http_requests_succeeded = 0
        self.http_requests_failed = 0
        self.http_failure_counts = collections.Counter()
        self.http_avg_duration_ms = 0.0
        self.timeout_count = 0
        self.network_error_count = 0
        self.outage_seconds = 0.0
        self.breaker_trips = 0
        self.breaker_half_open_count = 0
        self.breaker_recoveries = 0
        self.unpaired_http_requests = 0

        # ---------------- G. 本地持久化状态 (StateStore) ----------------
        self.local_submitting_count = 0
        self.local_unknown_count = 0
        self.local_verifying_count = 0
        self.local_retryable_count = 0
        self.local_unresolved_records: List[Dict] = []

    # =========================================================================
    # 数据注入与绑定接口
    # =========================================================================

    def bind_audit_logger(self, audit_logger: Any):
        """绑定统一审计日志记录器"""
        if audit_logger is not None:
            self.audit_bound = True
            if not self.run_id and hasattr(audit_logger, "run_id"):
                self.run_id = getattr(audit_logger, "run_id")
            if hasattr(audit_logger, "write_error_count"):
                self.write_error_count = int(getattr(audit_logger, "write_error_count", 0))
                if self.write_error_count > 0:
                    self.audit_degraded = True

    def set_audit_summary(self, summary: Dict[str, Any]):
        """
        设置来自 AuditLogReader.replay() 的权威审计回放汇总数据。
        这是运行过程指标（候选、提交、复核、底层 HTTP、熔断）的唯一事实来源。
        """
        if not isinstance(summary, dict):
            return

        self.audit_summary = summary
        self.audit_bound = True

        # 运行信息
        if not self.run_id and summary.get("run_id"):
            self.run_id = summary.get("run_id")

        # 候选评估
        self.candidate_count = summary.get("candidate_count", 0)
        self.candidate_accepted = summary.get("candidate_accepted", 0)
        self.candidate_rejected = summary.get("candidate_rejected", 0)
        self.candidate_blocked = summary.get("candidate_blocked", 0)
        self.candidate_reasons = dict(summary.get("candidate_reasons") or {})

        # 真实报名提交 (Submission, 严格不含 Verification)
        self.attempts_total = summary.get("submission_started_count", 0)
        self.explicit_success = summary.get("submission_succeeded_count", 0)
        self.explicit_failed = summary.get("submission_failed_count", 0)
        self.unknown_count = summary.get("submission_unknown_count", 0)

        # UNKNOWN 服务端复核 (Verification, 独立统计)
        self.verification_count = summary.get("verification_started_count", 0)
        self.verification_confirmed_registered = summary.get("verification_confirmed_registered_count", 0)
        self.verification_confirmed_unregistered = summary.get("verification_confirmed_unregistered_count", 0)
        self.verification_remains_unknown = summary.get("verification_remains_unknown_count", 0)
        self.verification_avg_duration_ms = float(summary.get("verification_avg_duration_ms", 0.0))

        # 底层 HTTP 请求统计
        self.http_requests_total = summary.get("http_requests_total", 0)
        self.http_requests_succeeded = summary.get("http_requests_succeeded", 0)
        self.http_requests_failed = summary.get("http_requests_failed", 0)
        breakdown = summary.get("http_failures_by_code") or {}
        self.http_failure_counts = collections.Counter({int(k): v for k, v in breakdown.items()})
        self.http_avg_duration_ms = float(summary.get("http_avg_duration_ms", 0.0))
        self.timeout_count = self.http_failure_counts.get(-1, 0)
        self.network_error_count = sum(cnt for code, cnt in self.http_failure_counts.items() if code < 0 or code >= 500)

        # 审计完整性与请求关联
        self.unpaired_http_requests = summary.get("unpaired_http_requests", 0)
        if summary.get("audit_degraded") or self.unpaired_http_requests > 0:
            self.audit_degraded = True

        # 熔断器指标
        self.breaker_trips = summary.get("circuit_trips", 0)
        self.breaker_half_open_count = summary.get("circuit_half_open_count", 0)
        self.breaker_recoveries = summary.get("circuit_closed_count", 0)
        if summary.get("outage_seconds") is not None:
            self.outage_seconds = float(summary.get("outage_seconds"))

    def set_reconciliation_result(
        self,
        reconciled_items: List[Dict],
        unresolved_count: int = 0,
        normal_registered: int = 0,
        max_normal: int = 2,
        special_registered: int = 0,
        max_per_day: int = 1,
        weekly_exceeded: bool = False,
        daily_exceeded: bool = False,
        reconciliation_report: Optional[Dict] = None,
        success: Optional[bool] = None
    ):
        """记录最终服务端对账结果（支持旧签名与新结构化报表）"""
        self.reconciliation_performed = True

        if reconciliation_report is not None:
            is_succ = bool(reconciliation_report.get("success", False))
            self.reconciliation_success = is_succ
            self.reconciliation_status = "SUCCESS" if is_succ else "UNVERIFIED"
            self.reconciliation_error = reconciliation_report.get("error")
            self.by_week_distribution = reconciliation_report.get("by_week") or {}
            self.by_day_distribution = reconciliation_report.get("by_day") or {}
            items = reconciliation_report.get("items") or reconciled_items or []
        else:
            is_succ = True if success is None else bool(success)
            self.reconciliation_success = is_succ
            self.reconciliation_status = "SUCCESS" if is_succ else "UNVERIFIED"
            items = reconciled_items or []

        self.reconciled_items = list(items)
        self.total_server_registered = len(self.reconciled_items)
        self.unresolved_count = unresolved_count

        # 区分本轮尝试确认成功与既有已报
        attempted_ids = {str(r.get("activity_id")) for r in self.attempt_records if r.get("activity_id")}
        if attempted_ids:
            this_run_confirmed = [it for it in self.reconciled_items if str(it.get("id")) in attempted_ids]
            self.this_run_success = len(this_run_confirmed)
            self.historical_count = max(0, len(self.reconciled_items) - self.this_run_success)
            self.reconciled_success = self.this_run_success
        else:
            self.this_run_success = len(self.reconciled_items)
            self.historical_count = 0
            self.reconciled_success = len(self.reconciled_items)

        # 确认未成功数量 = 总提交尝试 - 本轮确认成功 - 仍未解决
        self.reconciled_failed = max(0, self.attempts_total - self.reconciled_success - self.unresolved_count)

        self.normal_registered = normal_registered
        self.max_normal = max_normal
        self.special_registered = special_registered
        self.max_per_day = max_per_day
        self.weekly_limit_exceeded = weekly_exceeded
        self.daily_limit_exceeded = daily_exceeded

    def set_local_state_store(self, state_store: Any):
        """从 StateStore 加载当前持久化未决状态"""
        if state_store is None:
            return
        try:
            unresolved = state_store.get_unresolved_records()
            self.local_unresolved_records = list(unresolved)
            self.unresolved_count = max(self.unresolved_count, len(unresolved))

            for r in unresolved:
                st = (r.get("status") or "").upper()
                if st == "SUBMITTING":
                    self.local_submitting_count += 1
                elif st == "UNKNOWN":
                    self.local_unknown_count += 1
                elif st == "VERIFYING":
                    self.local_verifying_count += 1
                elif st == "RETRYABLE":
                    self.local_retryable_count += 1
        except Exception:
            pass

    # =========================================================================
    # 兼容历史调用的辅助记录方法
    # =========================================================================

    def record_attempt(self, activity_id: str, activity_name: str, status: str, reason: str = ""):
        """记录一次提交尝试（兼容旧测试与旧接口）"""
        self.attempts_total += 1
        st = status.upper()
        if st == "SUCCESS":
            self.explicit_success += 1
        elif st in ("FAILED", "RETRYABLE"):
            self.explicit_failed += 1
        elif st in ("UNKNOWN", "SUBMITTING", "VERIFYING"):
            self.unknown_count += 1

        self.attempt_records.append({
            "activity_id": str(activity_id),
            "activity_name": activity_name,
            "status": st,
            "reason": reason,
            "time": datetime.datetime.now().strftime("%H:%M:%S")
        })

    def record_http_failure(self, http_code: int):
        """记录一次 HTTP 故障（兼容旧接口）"""
        self.http_failure_counts[int(http_code)] += 1
        self.http_requests_failed += 1

    def set_circuit_metrics(self, outage_seconds: float, trip_count: int):
        """设置熔断器指标（兼容旧接口）"""
        self.outage_seconds = round(float(outage_seconds), 2)
        self.breaker_trips = int(trip_count)

    # =========================================================================
    # 核心结论状态机与综合汇总
    # =========================================================================

    def compute_final_verdict(self) -> Tuple[str, str]:
        """
        根据 Phase 4 规定的状态优先级决策树判定最终结论：
        1. 存在未决请求 (unresolved_count > 0) 或对账未成功 -> 必须输出 UNVERIFIED！
        2. 对账完全成功且无未决记录时：
           - 成功报名数 > 0 且无明确失败 -> SUCCESS
           - 成功报名数 > 0 但有部分失败或未满额 -> PARTIAL_SUCCESS
           - 成功报名数 == 0 -> FAILED
        """
        # 1. 存在未决请求，必须优先判定为 UNVERIFIED
        if self.unresolved_count > 0 or self.local_unknown_count > 0:
            return (
                ReportStatus.UNVERIFIED,
                f"存在 {self.unresolved_count} 节状态未决的报名请求，无法确认真实落地结果，安全屏障已阻断后续提交！"
            )

        # 2. 对账未执行、对账失败或对账列表数据残缺
        if not self.reconciliation_performed:
            return (
                ReportStatus.UNVERIFIED,
                "未执行最终全局服务端对账，无法确认实际报名状态。"
            )

        if not self.reconciliation_success or self.reconciliation_status != "SUCCESS":
            err_msg = self.reconciliation_error or "对账列表残缺或网络异常"
            return (
                ReportStatus.UNVERIFIED,
                f"最终服务端对账未通过 ({err_msg})，严禁将空列表解释为零报名，标记为未确认！"
            )

        # 3. 审计日志降级且有关键未配对事件
        if self.audit_degraded and (self.write_error_count > 0 or self.unpaired_http_requests > 0):
            if self.unpaired_http_requests > 0:
                return (
                    ReportStatus.UNVERIFIED,
                    f"审计事件存在 {self.unpaired_http_requests} 个未配对 HTTP 请求，底层审计数据不完整！"
                )

        # 4. 对账通过后的业务结果判定
        if self.reconciled_success > 0:
            if self.weekly_limit_exceeded or self.daily_limit_exceeded:
                return (
                    ReportStatus.PARTIAL_SUCCESS,
                    f"成功报名 {self.reconciled_success} 节，但检测到已超出配额限制（需人工核实退课）！"
                )
            if self.explicit_failed > 0 or self.reconciled_failed > 0:
                return (
                    ReportStatus.PARTIAL_SUCCESS,
                    f"成功报名 {self.reconciled_success} 节，但存在部分目标被拒绝或失败（详见提交明细）。"
                )
            return (
                ReportStatus.SUCCESS,
                f"成功报名 {self.reconciled_success} 节，全部限制合规，全局对账完全一致。"
            )

        # 成功数 == 0
        return (
            ReportStatus.FAILED,
            "本轮未成功报名任何活动（明确失败或名额已满）。"
        )

    def generate_summary(self) -> Dict[str, Any]:
        """
        生成结构化统计汇总字典。
        同时包含兼容旧版测试的关键字段与 Phase 4 增强的全新指标。
        """
        verdict_status, verdict_desc = self.compute_final_verdict()
        now_dt = datetime.datetime.now()
        duration_sec = round((now_dt - self.started_at).total_seconds(), 2)

        return {
            # 运行信息 (Run Info)
            "run_id": self.run_id or "run_unknown",
            "run_date": str(self.run_date),
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": (self.finished_at or now_dt).isoformat(timespec="seconds"),
            "duration_seconds": duration_sec,
            "current_week": self.current_week,
            "target_week": self.target_week,
            "audit_degraded": self.audit_degraded,
            "write_error_count": self.write_error_count,
            "unpaired_http_requests": self.unpaired_http_requests,

            # 候选评估 (Candidate)
            "candidates": {
                "total": self.candidate_count,
                "accepted": self.candidate_accepted,
                "rejected": self.candidate_rejected,
                "blocked": self.candidate_blocked,
                "reasons": dict(self.candidate_reasons)
            },

            # 真实报名提交 (Submission, 兼容 attempts)
            "attempts": {
                "total": self.attempts_total,
                "explicit_success": self.explicit_success,
                "explicit_failed": self.explicit_failed,
                "unknown": self.unknown_count,
                "records": list(self.attempt_records)
            },

            # UNKNOWN 服务端复核 (Verification)
            "verification": {
                "total": self.verification_count,
                "confirmed_registered": self.verification_confirmed_registered,
                "confirmed_unregistered": self.verification_confirmed_unregistered,
                "remains_unknown": self.verification_remains_unknown,
                "avg_duration_ms": self.verification_avg_duration_ms
            },

            # 服务端最终对账 (Reconciliation)
            "reconciliation": {
                "performed": self.reconciliation_performed,
                "success": self.reconciliation_success,
                "status": self.reconciliation_status,
                "confirmed_success": self.reconciled_success,
                "confirmed_failed": self.reconciled_failed,
                "unresolved": self.unresolved_count,
                "historical_count": self.historical_count,
                "total_server_registered": self.total_server_registered,
                "by_week": dict(self.by_week_distribution),
                "by_day": dict(self.by_day_distribution),
                "items": list(self.reconciled_items)
            },

            # 配额情况 (Quota)
            "quota": {
                "normal": f"{self.normal_registered} / {self.max_normal}",
                "normal_registered": self.normal_registered,
                "max_normal": self.max_normal,
                "special": self.special_registered,
                "max_per_day": self.max_per_day,
                "weekly_exceeded": self.weekly_limit_exceeded,
                "daily_exceeded": self.daily_limit_exceeded
            },

            # HTTP 请求与服务器状态 (Infrastructure, 兼容 server_failures)
            "server_failures": {
                "breakdown": dict(self.http_failure_counts),
                "total_failures": sum(self.http_failure_counts.values()) or self.http_requests_failed,
                "outage_seconds": self.outage_seconds,
                "breaker_trips": self.breaker_trips
            },
            "infrastructure": {
                "http_total": self.http_requests_total,
                "http_succeeded": self.http_requests_succeeded,
                "http_failed": self.http_requests_failed,
                "http_failures_breakdown": dict(self.http_failure_counts),
                "http_avg_duration_ms": self.http_avg_duration_ms,
                "timeout_count": self.timeout_count,
                "network_error_count": self.network_error_count,
                "circuit_trips": self.breaker_trips,
                "circuit_half_open": self.breaker_half_open_count,
                "circuit_recoveries": self.breaker_recoveries,
                "outage_seconds": self.outage_seconds
            },

            # 安全状态 (Security Barrier)
            "security_state": {
                "submitting_count": self.local_submitting_count,
                "unknown_count": self.local_unknown_count or self.unresolved_count,
                "verifying_count": self.local_verifying_count,
                "retryable_count": self.local_retryable_count,
                "has_unresolved": self.unresolved_count > 0 or self.local_unknown_count > 0,
                "has_write_errors": self.write_error_count > 0,
                "reconciliation_failed": not self.reconciliation_success if self.reconciliation_performed else True
            },

            # 最终结论 (Verdict)
            "verdict": {
                "status": verdict_status,
                "description": verdict_desc
            }
        }

    # =========================================================================
    # 控制台统一报告输出 (format_console_report)
    # =========================================================================

    def format_console_report(self) -> str:
        """
        格式化控制台输出文本。
        严格分为八大部分，使用 generate_summary() 统一数据源。
        """
        sm = self.generate_summary()
        verdict_status = sm["verdict"]["status"]
        verdict_desc = sm["verdict"]["description"]

        lines = []
        lines.append("=" * 60)
        lines.append("                    乐健体育抢课结果报告")
        lines.append("=" * 60)
        lines.append("")

        # 第一部分：运行信息
        lines.append("一、运行基本信息")
        lines.append(f"  运行编号 (run_id) ：{sm['run_id']}")
        lines.append(f"  运行日期         ：{sm['run_date']}")
        lines.append(f"  开始时间         ：{sm['started_at']}")
        lines.append(f"  结束时间         ：{sm['finished_at']}")
        lines.append(f"  运行时长         ：{sm['duration_seconds']} 秒")
        if sm["current_week"] > 0:
            lines.append(f"  当前教学周       ：第 {sm['current_week']} 周")
        if sm["target_week"] > 0:
            lines.append(f"  目标教学周       ：第 {sm['target_week']} 周")
        audit_tag = "⚠️ 降级 (存在写入错误或未配对请求)" if sm["audit_degraded"] else "完整"
        lines.append(f"  审计日志完整性   ：{audit_tag}")
        lines.append("")

        # 第二部分：候选评估
        cand = sm["candidates"]
        lines.append("二、候选活动评估")
        lines.append(f"  候选评估总数     ：{cand['total']} 次")
        lines.append(f"  通过评估         ：{cand['accepted']} 次")
        lines.append(f"  规则拦截         ：{cand['rejected']} 次")
        if cand["blocked"] > 0:
            lines.append(f"  屏障阻断         ：{cand['blocked']} 次")
        if cand["reasons"]:
            lines.append("  主要拦截原因     ：")
            for r_txt, cnt in sorted(cand["reasons"].items(), key=lambda x: -x[1])[:3]:
                lines.append(f"    - {r_txt}：{cnt} 次")
        lines.append("")

        # 第三部分：真实报名提交 (Submission)
        att = sm["attempts"]
        lines.append("三、真实报名提交 (Submission)")
        lines.append(f"  真实提交次数     ：{att['total']} 次")
        lines.append(f"  明确成功 (Success)：{att['explicit_success']} 次")
        lines.append(f"  明确失败 (Failed) ：{att['explicit_failed']} 次")
        lines.append(f"  未决状态 (UNKNOWN)：{att['unknown']} 次")
        lines.append("")

        # 第四部分：UNKNOWN 复核 (Verification)
        ver = sm["verification"]
        lines.append("四、UNKNOWN 服务端复核 (Verification)")
        lines.append(f"  复核总调用次数   ：{ver['total']} 次")
        lines.append(f"  复核确认已报     ：{ver['confirmed_registered']} 次")
        lines.append(f"  复核确认未报     ：{ver['confirmed_unregistered']} 次")
        lines.append(f"  复核仍未解决     ：{ver['remains_unknown']} 次")
        if ver["avg_duration_ms"] > 0:
            lines.append(f"  平均复核耗时     ：{ver['avg_duration_ms']} ms")
        lines.append("")

        # 第五部分：服务端最终对账 (Reconciliation)
        rec = sm["reconciliation"]
        lines.append("五、服务端最终对账 (Reconciliation)")
        rec_st_desc = "通过 (SUCCESS)" if rec["status"] == "SUCCESS" else f"未确认 ({rec['status']})"
        lines.append(f"  最终对账状态     ：{rec_st_desc}")
        lines.append(f"  确认报名成功     ：{rec['confirmed_success']} 节")
        lines.append(f"  确认未报名       ：{rec['confirmed_failed']} 节")
        lines.append(f"  尚未解决 (未决)  ：{rec['unresolved']} 节")
        if rec["historical_count"] > 0:
            lines.append(f"  既有已报活动：{rec['historical_count']} 节（服务端当前总已报：{rec['total_server_registered']} 节）")
        q = sm["quota"]
        lines.append(f"  普通活动名额     ：{q['normal']}")
        lines.append(f"  特殊活动名额     ：{q['special']}")
        if q["daily_exceeded"] or q["weekly_exceeded"]:
            lines.append("  配额超额告警     ：⚠️ 检测到超额报名！")
        lines.append("")

        # 第六部分：HTTP 请求与服务器状态
        inf = sm["infrastructure"]
        lines.append("六、HTTP 请求与基础设施状态")
        lines.append(f"  HTTP 请求总数    ：{inf['http_total']} 次")
        lines.append(f"  HTTP 成功次数    ：{inf['http_succeeded']} 次")
        lines.append(f"  HTTP 失败总计    ：{inf['http_failed']} 次")
        if inf["http_failures_breakdown"]:
            lines.append("  失败状态码分布   ：")
            for code, cnt in sorted(inf["http_failures_breakdown"].items()):
                c_lbl = f"HTTP {code}" if code > 0 else "HTTP -1 (超时)"
                lines.append(f"    - {c_lbl}：{cnt} 次")
        if inf["http_avg_duration_ms"] > 0:
            lines.append(f"  平均请求耗时     ：{inf['http_avg_duration_ms']} ms")
        lines.append(f"  熔断触发次数     ：{inf['circuit_trips']} 次")
        lines.append(f"  半开探测次数     ：{inf['circuit_half_open']} 次")
        lines.append(f"  累计服务中断     ：{inf['outage_seconds']:.1f} 秒")
        lines.append("")

        # 第七部分：安全状态
        sec = sm["security_state"]
        lines.append("七、安全屏障与持久化状态")
        lines.append(f"  SUBMITTING 数量  ：{sec['submitting_count']}")
        lines.append(f"  UNKNOWN 数量     ：{sec['unknown_count']}")
        lines.append(f"  VERIFYING 数量   ：{sec['verifying_count']}")
        lines.append(f"  RETRYABLE 数量   ：{sec['retryable_count']}")
        lines.append(f"  是否存在未决请求 ：{'⚠️ 是' if sec['has_unresolved'] else '否'}")
        lines.append(f"  审计日志写入失败 ：{'⚠️ 是' if sec['has_write_errors'] else '否'}")
        lines.append("")

        # 第八部分：最终结论
        lines.append("八、最终结论")
        lines.append(f"  结论判定         ：[{verdict_status}]")
        lines.append(f"  说明             ：{verdict_desc}")

        if verdict_status == ReportStatus.UNVERIFIED:
            # 严格向后兼容 R06 断言
            lines.append("")
            lines.append("最终状态：未完成对账 (存在未决 UNKNOWN)")
            lines.append("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            lines.append("【安全警告】")
            lines.append("当前仍有未解决的报名请求或对账未通过，无法确认最终报名数量。")
            lines.append("请勿盲目重新提交该活动。下次启动时将优先恢复并复核该请求。")
            lines.append("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        elif verdict_status in (ReportStatus.SUCCESS, ReportStatus.PARTIAL_SUCCESS):
            lines.append(f"  落地结果         ：成功报名 {rec['confirmed_success']} 节")
        else:
            lines.append("  落地结果         ：未成功报名活动")

        lines.append("=" * 60)
        return "\n".join(lines)

    # =========================================================================
    # Markdown 统一报告生成与防覆盖保存 (save_markdown_report)
    # =========================================================================

    def save_markdown_report(self, report_dir: str = "logs/reports", base_name: Optional[str] = None) -> str:
        """
        生成并安全保存 Markdown 报告文件。
        如果目标文件已存在，采用安全防覆盖重命名规则 (R12)。
        返回保存的文件绝对路径。
        """
        os.makedirs(report_dir, exist_ok=True)
        date_str = str(self.run_date)
        file_prefix = base_name or f"{date_str}-report"

        target_path = os.path.join(report_dir, f"{file_prefix}.md")
        if os.path.exists(target_path):
            idx = 1
            while True:
                candidate = os.path.join(report_dir, f"{file_prefix}_{idx}.md")
                if not os.path.exists(candidate):
                    target_path = candidate
                    break
                idx += 1

        md_content = self._build_markdown_content()
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(md_content)

        return os.path.abspath(target_path)

    def _build_markdown_content(self) -> str:
        """生成 Markdown 报告正文（与控制台数据源 100% 同源）"""
        sm = self.generate_summary()
        verdict_status = sm["verdict"]["status"]
        verdict_desc = sm["verdict"]["description"]
        rec = sm["reconciliation"]
        att = sm["attempts"]
        inf = sm["infrastructure"]
        q = sm["quota"]
        sec = sm["security_state"]

        lines = []
        lines.append(f"# 抢课结果报告 ({self.run_date})")
        lines.append("")
        lines.append(f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  |  Run ID: `{sm['run_id']}`")
        lines.append("")

        # 一、运行基本信息
        lines.append("## 一、运行基本信息")
        lines.append(f"- **运行日期**：{sm['run_date']}")
        lines.append(f"- **运行编号**：`{sm['run_id']}`")
        lines.append(f"- **开始时间**：{sm['started_at']}")
        lines.append(f"- **结束时间**：{sm['finished_at']}")
        lines.append(f"- **运行时长**：{sm['duration_seconds']} 秒")
        if sm["current_week"] > 0:
            lines.append(f"- **当前教学周**：第 {sm['current_week']} 周")
        if sm["target_week"] > 0:
            lines.append(f"- **目标教学周**：第 {sm['target_week']} 周")
        audit_tag = "⚠️ 降级 (存在写入错误或未配对请求)" if sm["audit_degraded"] else "✅ 完整可靠"
        lines.append(f"- **审计日志完整性**：{audit_tag}")
        lines.append("")

        # 二、候选评估
        cand = sm["candidates"]
        lines.append("## 二、候选活动评估")
        lines.append(f"- **候选评估总数**：{cand['total']}")
        lines.append(f"- **通过规则校验**：{cand['accepted']}")
        lines.append(f"- **规则前置拦截**：{cand['rejected']}")
        if cand["blocked"] > 0:
            lines.append(f"- **安全屏障阻断**：{cand['blocked']}")
        if cand["reasons"]:
            lines.append("### 拦截原因细分")
            for r_txt, cnt in sorted(cand["reasons"].items(), key=lambda x: -x[1]):
                lines.append(f"- {r_txt}：{cnt} 次")
        lines.append("")

        # 三、真实报名提交 (Submission)
        lines.append("## 三、真实报名提交 (Submission)")
        lines.append(f"- **提交尝试总数**：{att['total']}")
        lines.append(f"- **明确成功 (Explicit Success)**：{att['explicit_success']}")
        lines.append(f"- **明确失败 (Explicit Failed)**：{att['explicit_failed']}")
        lines.append(f"- **未知状态 (UNKNOWN)**：{att['unknown']}")
        lines.append("")
        if att["records"]:
            lines.append("| 序号 | 时刻 | 活动名称 | 提交结果 | 详情 |")
            lines.append("|---|---|---|---|---|")
            for idx, r in enumerate(att["records"], 1):
                lines.append(f"| {idx} | {r.get('time', '-')} | {r.get('activity_name', '-')} | `{r.get('status', '-')}` | {r.get('reason', '-')} |")
            lines.append("")

        # 四、UNKNOWN 服务端复核 (Verification)
        ver = sm["verification"]
        lines.append("## 四、UNKNOWN 服务端复核 (Verification)")
        lines.append(f"- **复核调用总数**：{ver['total']}")
        lines.append(f"- **复核确认已报**：{ver['confirmed_registered']}")
        lines.append(f"- **复核确认未报**：{ver['confirmed_unregistered']}")
        lines.append(f"- **复核仍未解决**：{ver['remains_unknown']}")
        if ver["avg_duration_ms"] > 0:
            lines.append(f"- **平均复核耗时**：{ver['avg_duration_ms']} ms")
        lines.append("")

        # 五、服务端最终对账
        lines.append("## 五、服务端最终对账 (Reconciliation)")
        st_tag = "✅ 通过" if rec["status"] == "SUCCESS" else f"⚠️ {rec['status']}"
        lines.append(f"- **对账结果**：{st_tag}")
        lines.append(f"- **服务端确认报名成功**：{rec['confirmed_success']} 节")
        if rec["historical_count"] > 0:
            lines.append(f"- **既有/历史已报活动**：{rec['historical_count']} 节（总计已报：{rec['total_server_registered']} 节）")
        lines.append(f"- **确认未报名**：{rec['confirmed_failed']} 节")
        lines.append(f"- **尚未解决 (UNRESOLVED)**：{rec['unresolved']} 节")
        lines.append("")
        if rec["items"]:
            lines.append("### 已确认落地活动清单")
            for it in rec["items"]:
                st_ms = int(it.get("startTime") or 0)
                d = datetime.datetime.fromtimestamp(st_ms / 1000) if st_ms else None
                time_str = f"{d:%m-%d %H:%M}" if d else "未知时间"
                lines.append(f"- **{time_str}**：{it.get('name')} (ID: `{it.get('id')}`)")
            lines.append("")

        # 六、配额与限制合规
        lines.append("## 六、配额与限制合规")
        lines.append(f"- **普通活动名额**：{q['normal']}")
        lines.append(f"- **特殊活动名额**：{q['special']}")
        lines.append(f"- **每日上限校验**：{'⚠️ 存在超额' if q['daily_exceeded'] else '✅ 正常未超额'}")
        lines.append(f"- **每周上限校验**：{'⚠️ 存在超额' if q['weekly_exceeded'] else '✅ 正常未超额'}")
        lines.append("")

        # 七、服务器故障与熔断统计
        lines.append("## 七、服务器故障与熔断统计")
        lines.append(f"- **HTTP 请求总数**：{inf['http_total']} 次")
        lines.append(f"- **HTTP 成功总数**：{inf['http_succeeded']} 次")
        lines.append(f"- **请求异常总数**：{inf['http_failed']} 次")
        if inf["http_failures_breakdown"]:
            for code, cnt in sorted(inf["http_failures_breakdown"].items()):
                label = f"HTTP {code}" if code > 0 else "HTTP -1 (超时)"
                lines.append(f"  - {label}：{cnt} 次")
        lines.append(f"- **累计服务中断**：{inf['outage_seconds']:.1f} 秒")
        lines.append(f"- **熔断器触发次数**：{inf['circuit_trips']} 次")
        lines.append("")

        # 八、最终结论
        lines.append("## 八、最终结论")
        if verdict_status == ReportStatus.UNVERIFIED:
            lines.append("> ⚠️ **警告：存在未决 UNKNOWN 或最终对账未完成！**")
            lines.append(f"> 原因：{verdict_desc}")
            lines.append("> 当前无法确认真实报名数量，严禁重复提交。下次启动将优先恢复并复核该请求。")
        elif verdict_status == ReportStatus.PARTIAL_SUCCESS:
            lines.append(f"> ⚠️ **部分成功：确认落地 {rec['confirmed_success']} 节。**")
            lines.append(f"> 说明：{verdict_desc}")
        elif verdict_status == ReportStatus.SUCCESS:
            lines.append(f"> ✅ **抢课成功落地 {rec['confirmed_success']} 节，全部限制合规，全局对账完全通过。**")
        else:
            lines.append("> ❌ **抢课未成功：未获取到有效名额。**")
            lines.append(f"> 说明：{verdict_desc}")

        return "\n".join(lines)
