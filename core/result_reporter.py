"""
抢课结果统计与报告生成模块 (ResultReporter)
===========================================
严格区分「提交过程统计」与「最终服务端对账结果」，彻底杜绝口径割裂。
当存在未解决的 UNKNOWN 时明确提示警示，并在 logs/reports/ 自动生成 Markdown 报告文件。
"""
import collections
import datetime
import os
from typing import Dict, List, Optional


class ResultReporter:
    """抢课结果统计器与报告生成器"""

    def __init__(self, run_date: Optional[datetime.date] = None):
        self.run_date = run_date or datetime.date.today()
        self.started_at = datetime.datetime.now()
        self.current_week = 0
        self.target_week = 0

        # A. 提交过程统计
        self.attempts_total = 0
        self.explicit_success = 0
        self.explicit_failed = 0
        self.unknown_count = 0
        self.attempt_records: List[Dict] = []

        # B. 服务端对账结果
        self.reconciled_success = 0
        self.reconciled_failed = 0
        self.unresolved_count = 0
        self.reconciled_items: List[Dict] = []
        self.reconciliation_performed = False

        # C. 配额情况
        self.normal_registered = 0
        self.max_normal = 2
        self.special_registered = 0
        self.max_per_day = 1
        self.daily_limit_exceeded = False
        self.weekly_limit_exceeded = False

        # D. 网络与故障统计
        self.http_failure_counts = collections.Counter()
        self.outage_seconds = 0.0
        self.breaker_trips = 0

    def record_attempt(self, activity_id: str, activity_name: str, status: str, reason: str = ""):
        """记录一次提交尝试及即时状态"""
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
        """记录一次 HTTP/网络故障（如 -1 超时, 500, 503, 504）"""
        self.http_failure_counts[int(http_code)] += 1

    def set_reconciliation_result(
        self,
        reconciled_items: List[Dict],
        unresolved_count: int = 0,
        normal_registered: int = 0,
        max_normal: int = 2,
        special_registered: int = 0,
        max_per_day: int = 1,
        weekly_exceeded: bool = False,
        daily_exceeded: bool = False
    ):
        """记录最终服务端对账结果"""
        self.reconciliation_performed = True
        self.reconciled_items = list(reconciled_items)
        self.reconciled_success = len(reconciled_items)
        self.unresolved_count = unresolved_count

        # 确认未成功数量 = 总提交尝试 - 最终确认成功 - 仍未解决
        self.reconciled_failed = max(0, self.attempts_total - self.reconciled_success - self.unresolved_count)

        self.normal_registered = normal_registered
        self.max_normal = max_normal
        self.special_registered = special_registered
        self.max_per_day = max_per_day
        self.weekly_limit_exceeded = weekly_exceeded
        self.daily_limit_exceeded = daily_exceeded

    def set_circuit_metrics(self, outage_seconds: float, trip_count: int):
        """设置熔断器与累计中断指标"""
        self.outage_seconds = round(outage_seconds, 2)
        self.breaker_trips = trip_count

    def generate_summary(self) -> Dict:
        """生成结构化统计汇总字典"""
        return {
            "run_date": str(self.run_date),
            "current_week": self.current_week,
            "target_week": self.target_week,
            "attempts": {
                "total": self.attempts_total,
                "explicit_success": self.explicit_success,
                "explicit_failed": self.explicit_failed,
                "unknown": self.unknown_count
            },
            "reconciliation": {
                "performed": self.reconciliation_performed,
                "confirmed_success": self.reconciled_success,
                "confirmed_failed": self.reconciled_failed,
                "unresolved": self.unresolved_count
            },
            "quota": {
                "normal": f"{self.normal_registered} / {self.max_normal}",
                "special": self.special_registered,
                "weekly_exceeded": self.weekly_limit_exceeded,
                "daily_exceeded": self.daily_limit_exceeded
            },
            "server_failures": {
                "breakdown": dict(self.http_failure_counts),
                "total_failures": sum(self.http_failure_counts.values()),
                "outage_seconds": self.outage_seconds,
                "breaker_trips": self.breaker_trips
            }
        }

    def format_console_report(self) -> str:
        """格式化控制台输出文本"""
        lines = []
        lines.append("=" * 50)
        lines.append("                  抢课结果报告")
        lines.append("=" * 50)
        lines.append("")
        lines.append("一、基本信息")
        lines.append(f"运行日期：{self.run_date}")
        if self.current_week > 0:
            lines.append(f"当前教学周：第 {self.current_week} 周")
        if self.target_week > 0:
            lines.append(f"目标教学周：第 {self.target_week} 周")
        lines.append("运行模式：单进程")
        lines.append("")

        lines.append("二、提交过程统计")
        lines.append(f"提交尝试：{self.attempts_total} 次")
        lines.append(f"明确成功：{self.explicit_success} 次")
        lines.append(f"明确失败：{self.explicit_failed} 次")
        lines.append(f"UNKNOWN ：{self.unknown_count} 次")
        lines.append("")

        lines.append("三、服务端最终对账")
        lines.append(f"确认报名成功：{self.reconciled_success} 节")
        lines.append(f"确认未报名  ：{self.reconciled_failed} 节")
        lines.append(f"尚未解决    ：{self.unresolved_count} 节")
        lines.append("")

        lines.append("四、实际报名情况")
        lines.append(f"普通活动：{self.normal_registered} / {self.max_normal}")
        lines.append(f"特殊活动：{self.special_registered}")
        lines.append(f"每日限制：{'已超出警告!' if self.daily_limit_exceeded else '未超出'}")
        lines.append(f"每周限制：{'已超出警告!' if self.weekly_limit_exceeded else '未超出'}")
        lines.append("")

        lines.append("五、服务器异常与熔断")
        tot_err = sum(self.http_failure_counts.values())
        lines.append(f"请求失败总计：{tot_err} 次")
        for code, cnt in sorted(self.http_failure_counts.items()):
            label = f"HTTP {code}" if code > 0 else "HTTP -1(超时)"
            lines.append(f"  {label}：{cnt} 次")
        lines.append(f"累计故障中断：{self.outage_seconds:.1f} 秒")
        lines.append(f"熔断触发次数：{self.breaker_trips} 次")
        lines.append("")

        lines.append("六、最终状态")
        if self.unresolved_count > 0:
            # 5.6: 未完成对账时严禁输出抢课成功 0 节，必须输出明确警告
            lines.append("最终状态：未完成对账 (存在未决 UNKNOWN)")
            lines.append("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            lines.append("【安全警告】")
            lines.append("当前仍有未解决的报名请求，无法确认最终报名数量。")
            lines.append("请勿重新提交该活动。下次启动时将优先恢复并复核该请求。")
            lines.append("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        else:
            lines.append(f"服务端对账：{'通过' if not self.weekly_limit_exceeded else '超额报警'}")
            lines.append(f"最终结果：成功报名 {self.reconciled_success} 节")

        lines.append("=" * 50)
        return "\n".join(lines)

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
        lines = []
        lines.append(f"# 抢课结果报告 ({self.run_date})")
        lines.append("")
        lines.append(f"> 生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
        lines.append("")
        lines.append("## 一、基本信息")
        lines.append(f"- **运行日期**：{self.run_date}")
        if self.current_week > 0:
            lines.append(f"- **当前教学周**：第 {self.current_week} 周")
        if self.target_week > 0:
            lines.append(f"- **目标教学周**：第 {self.target_week} 周")
        lines.append("- **运行模式**：单进程 (带 UNKNOWN 安全屏障与熔断机制)")
        lines.append("")

        lines.append("## 二、提交过程统计")
        lines.append(f"- **提交尝试总数**：{self.attempts_total}")
        lines.append(f"- **明确成功 (Explicit Success)**：{self.explicit_success}")
        lines.append(f"- **明确失败 (Explicit Failed)**：{self.explicit_failed}")
        lines.append(f"- **未知状态 (UNKNOWN)**：{self.unknown_count}")
        lines.append("")
        if self.attempt_records:
            lines.append("| 序号 | 时刻 | 活动名称 | 提交结果 | 详情 |")
            lines.append("|---|---|---|---|---|")
            for idx, r in enumerate(self.attempt_records, 1):
                lines.append(f"| {idx} | {r['time']} | {r['activity_name']} | `{r['status']}` | {r['reason']} |")
            lines.append("")

        lines.append("## 三、服务端最终对账")
        lines.append(f"- **服务端确认报名成功**：{self.reconciled_success} 节")
        lines.append(f"- **确认未报名**：{self.reconciled_failed} 节")
        lines.append(f"- **尚未解决 (UNRESOLVED)**：{self.unresolved_count} 节")
        lines.append("")
        if self.reconciled_items:
            lines.append("### 已确认落地活动清单")
            for it in self.reconciled_items:
                st_ms = int(it.get("startTime") or 0)
                d = datetime.datetime.fromtimestamp(st_ms / 1000) if st_ms else None
                time_str = f"{d:%m-%d %H:%M}" if d else "未知时间"
                lines.append(f"- **{time_str}**：{it.get('name')} (ID: `{it.get('id')}`)")
            lines.append("")

        lines.append("## 四、配额与限制合规")
        lines.append(f"- **普通活动名额**：{self.normal_registered} / {self.max_normal}")
        lines.append(f"- **特殊活动名额**：{self.special_registered}")
        lines.append(f"- **每日上限校验**：{'⚠️ 存在超额' if self.daily_limit_exceeded else '✅ 正常未超额'}")
        lines.append(f"- **每周上限校验**：{'⚠️ 存在超额' if self.weekly_limit_exceeded else '✅ 正常未超额'}")
        lines.append("")

        lines.append("## 五、服务器故障与熔断统计")
        lines.append(f"- **请求异常总数**：{sum(self.http_failure_counts.values())} 次")
        for code, cnt in sorted(self.http_failure_counts.items()):
            label = f"HTTP {code}" if code > 0 else "HTTP -1 (超时)"
            lines.append(f"  - {label}：{cnt} 次")
        lines.append(f"- **累计服务中断**：{self.outage_seconds:.1f} 秒")
        lines.append(f"- **熔断器触发次数**：{self.breaker_trips} 次")
        lines.append("")

        lines.append("## 六、最终结论")
        if self.unresolved_count > 0:
            lines.append("> ⚠️ **警告：存在未决 UNKNOWN，未完成最终对账。**")
            lines.append("> 当前无法确认真实报名数量，严禁重复提交。下次启动将优先恢复并复核该请求。")
        elif self.weekly_limit_exceeded or self.daily_limit_exceeded:
            lines.append("> ⚠️ **警告：检测到报名数量超额，请通过手动退课确认！**")
        else:
            lines.append(f"✅ **抢课成功落地 {self.reconciled_success} 节，全部限制合规，全局对账通过。**")

        return "\n".join(lines)
