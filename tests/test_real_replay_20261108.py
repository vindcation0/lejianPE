"""
Phase 8: 2026-11-08 真实事件回放与审计报告数据闭环测试套件
==============================================================
基于 2026-11-08 抢课实战脱敏事件序列，验证数据闭环与报告口径修复：
1. 连续活动列表失败与多种 HTTP 状态码 (-1: 8次, 500: 8次, 502: 1次, 503: 1次，合计 18 次)。
2. 多次服务恢复与累计中断时长 (7 次恢复，累计 49.0 秒)。
3. UNKNOWN 产生、复核未报名与复核成功分支。
4. 真实提交调用 (6 次) 与 UNKNOWN 只读复核 (2 次) 严格物理分离，杜绝虚增至 8 次。
5. 最终服务端全局对账 (2 节落地：王彤帆、周天一，无未决记录)。
6. 原始事件去重幂等性 (Seen Event IDs)。
7. 审计事实单向优先性 (set_circuit_metrics 绝不覆盖 AuditLogReader 的真实统计)。
8. checkConflict 失败与 activities 列表失败独立统计。
"""
import collections
import datetime
import json
import os
import tempfile
import unittest

from core.audit_logger import AuditLogger, AuditLogReader, EventName, EventType
from core.circuit_breaker import CircuitBreaker
from core.result_reporter import ReportStatus, ResultReporter
from core.state_store import RegistrationStatus, StateStore

# 2026-11-08 抢课实战脱敏事件序列 (13:58:58 ~ 14:11:38)
RAW_EVENTS_20261108 = [
    {"t": "2026-11-08T13:58:58", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:02:51", "event": "list_fail", "http": 502},
    {"t": "2026-11-08T14:02:58", "event": "list_fail", "http": 503},
    {"t": "2026-11-08T14:03:48", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:03:56", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:04:31", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:05:04", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:05:37", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:05:51", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:06:58", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:07:18", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:07:23", "event": "list_ok", "outage_sec": 5},
    {"t": "2026-11-08T14:08:03", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:08:04", "event": "list_ok", "outage_sec": 1},
    {"t": "2026-11-08T14:08:22", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:08:30", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:08:50", "event": "list_ok", "outage_sec": 21},
    {"t": "2026-11-08T14:08:57", "event": "resolve_unknown_not_registered", "aid": "act_wang", "name": "户外跑 王彤帆"},
    {"t": "2026-11-08T14:09:46", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:10:05", "event": "list_ok", "outage_sec": 19},
    {"t": "2026-11-08T14:11:06", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:11:07", "event": "list_ok", "outage_sec": 1},
    {"t": "2026-11-08T14:11:07", "event": "resolve_unknown_not_registered", "aid": "act_cheng", "name": "户外跑 程启航"},
    {"t": "2026-11-08T14:11:09", "event": "list_fail", "http": 500},
    {"t": "2026-11-08T14:11:10", "event": "list_ok", "outage_sec": 1},
    {"t": "2026-11-08T14:11:23", "event": "list_fail", "http": -1},
    {"t": "2026-11-08T14:11:23", "event": "resolve_unknown_failed", "aid": "act_cheng", "name": "户外跑 程启航"},
    {"t": "2026-11-08T14:11:24", "event": "list_ok", "outage_sec": 1},
    {"t": "2026-11-08T14:11:38", "event": "resolve_unknown_not_registered", "aid": "act_cheng", "name": "户外跑 程启航"},
]


class TestRealReplay20261108(unittest.TestCase):
    """2026-11-08 实战事件回放与数据闭环专项验收测试"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logs_dir = os.path.join(self.temp_dir.name, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.event_file = os.path.join(self.logs_dir, "events-20261108.jsonl")

        # 将真实脱敏事件写入临时文件
        with open(self.event_file, "w", encoding="utf-8") as f:
            for item in RAW_EVENTS_20261108:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    # ---------------- 1. 真实故障事件回放基础统计 ----------------
    def test_01_replay_audit_log_from_raw_sanitized_events(self):
        """验证 2026-11-08 原始脱敏日志回放的准确性 (18次失败, 49.0s 中断)"""
        events = AuditLogReader.read_events_from_file(self.event_file)
        self.assertEqual(len(events), 29)

        # 回放指定实战时间窗口
        summary = AuditLogReader.replay(
            events,
            start_time="2026-11-08T13:58:00",
            end_time="2026-11-08T14:12:00"
        )

        # 核心指标断言
        self.assertEqual(summary["total_events"], 29)
        self.assertEqual(summary["http_requests_failed"], 18, "活动列表 HTTP 失败总数必须精准为 18 次")
        self.assertEqual(summary["outage_seconds"], 49.0, "故障累计中断时间必须精准为 49.0 秒")

        # 状态码分布断言 (-1: 8次, 500: 8次, 502: 1次, 503: 1次)
        expected_breakdown = {-1: 8, 500: 8, 502: 1, 503: 1}
        self.assertEqual(summary["http_failures_by_code"], expected_breakdown)
        self.assertEqual(summary["timeout_count"], 8)
        self.assertEqual(summary["network_error_count"], 18)

        # 服务恢复次数断言 (7 次恢复)
        self.assertEqual(summary["circuit_closed_count"], 7)

        # UNKNOWN 复核分类断言 (3 次确认未报, 1 次无法确认保持未决)
        self.assertEqual(summary["verification_confirmed_unregistered_count"], 3)
        self.assertEqual(summary["verification_remains_unknown_count"], 1)

    # ---------------- 2. 事件读取去重与幂等性 ----------------
    def test_02_idempotent_event_replay_no_double_counting(self):
        """验证重复读取同一日志或同一事件不会产生二次计数 (A13 幂等性)"""
        events = AuditLogReader.read_events_from_file(self.event_file)
        # 复制 3 遍构建包含重复 event_id 的流
        tripled_stream = events + events + events

        summary = AuditLogReader.replay(tripled_stream)
        self.assertEqual(summary["total_events"], 29, "重复事件必须完全去重")
        self.assertEqual(summary["http_requests_failed"], 18)
        self.assertEqual(summary["outage_seconds"], 49.0)

    # ---------------- 3. 提交与复核严格分离验证 ----------------
    def test_03_submission_and_verification_separation_real_data(self):
        """验证 11 月 8 日第二实例中 6 次真实提交与 2 次复核完全解耦，提交总数不再虚增为 8"""
        reporter = ResultReporter(run_date=datetime.date(2026, 11, 8), run_id="run_20261108_inst2")

        # 模拟 11 月 8 日第二实例实际发生的 6 次真实 signUp 提交
        reporter.record_attempt("act_cheng", "户外跑 程启航", "UNKNOWN", "提交异常且无法复核状态(请求超时)")
        reporter.record_attempt("act_cheng", "户外跑 程启航", "UNKNOWN", "提交异常且无法复核状态(网关或服务异常 HTTP 500)")
        reporter.record_attempt("act_cheng", "户外跑 程启航", "FAILED", "报名人数已经满")
        reporter.record_attempt("act_wang", "户外跑 王彤帆", "SUCCESS", "报名成功")
        reporter.record_attempt("act_liao", "户外跑 廖华", "RETRYABLE", "提交异常(请求超时)，复核确认未报名")
        reporter.record_attempt("act_zhou", "户外跑 周天一", "SUCCESS", "报名成功")

        # 模拟安全屏障在两次 UNKNOWN 后执行的 2 次只读复核
        reporter.record_verification("act_cheng", "户外跑 程启航", "RETRYABLE", "复核确认未报名")
        reporter.record_verification("act_cheng", "户外跑 程启航", "RETRYABLE", "复核确认未报名")

        # 即使有旧代码尝试将复核作为 attempt 传入，防御机制自动分流
        reporter.record_attempt("act_cheng", "户外跑 程启航", "FAILED", "复核确认未报名")

        sm = reporter.generate_summary()
        self.assertEqual(sm["attempts"]["total"], 6, "真实提交尝试总数必须精准为 6 次，绝不能为 8 次！")
        self.assertEqual(len(sm["attempts"]["records"]), 6, "提交明细表格必须仅展示真实的 6 次提交")
        self.assertEqual(sm["attempts"]["explicit_success"], 2)
        self.assertEqual(sm["attempts"]["explicit_failed"], 2)  # 1次人数满 + 1次廖华重试态
        self.assertEqual(sm["attempts"]["unknown"], 2)

        self.assertEqual(sm["verification"]["total"], 3, "复核调用总数独立统计")
        self.assertEqual(sm["verification"]["confirmed_unregistered"], 3)

    # ---------------- 4. 审计事实单向优先性 ----------------
    def test_04_set_circuit_metrics_preserves_audit_truth(self):
        """验证展示层 set_circuit_metrics 绝不覆盖由 AuditLogReader 计算出的权威事实"""
        reporter = ResultReporter(run_date=datetime.date(2026, 11, 8), run_id="run_20261108_audit")

        events = AuditLogReader.read_events_from_file(self.event_file)
        audit_summary = AuditLogReader.replay(events, start_time="2026-11-08T13:58:00", end_time="2026-11-08T14:12:00")
        reporter.set_audit_summary(audit_summary)

        # 校验注入后的初始中断时间
        self.assertEqual(reporter.outage_seconds, 49.0)

        # 模拟实例重启后内存 CircuitBreaker 指标全为 0 并调用 set_circuit_metrics
        cb = CircuitBreaker()
        cb_metrics = cb.get_metrics()
        self.assertEqual(cb_metrics["total_outage_seconds"], 0.0)
        self.assertEqual(cb_metrics["trip_count"], 0)

        reporter.set_circuit_metrics(
            outage_seconds=cb_metrics["total_outage_seconds"],
            trip_count=cb_metrics["trip_count"]
        )

        # 权威性断言：49.0s 必须完整保留，绝不能被 0.0s 覆盖
        self.assertEqual(reporter.outage_seconds, 49.0, "内存零值熔断器指标禁止覆盖审计回放得出的 49.0 秒！")

    # ---------------- 5. 11月8日真实基准端到端报告生成验收 ----------------
    def test_05_end_to_end_report_matches_20261108_ground_truth(self):
        """端到端验证 2026-11-08 抢课报告：18次失败、49.0s中断、6次提交、2节成功落地、对账通过"""
        reporter = ResultReporter(run_date=datetime.date(2026, 11, 8), run_id="run_20261108_final")

        # 1. 注入真实 6 次提交明细（模拟抢课运行时各次提交）
        reporter.record_attempt("act_cheng", "户外跑 程启航", "UNKNOWN", "提交异常且无法复核状态(请求超时)")
        reporter.record_attempt("act_cheng", "户外跑 程启航", "UNKNOWN", "提交异常且无法复核状态(HTTP 500)")
        reporter.record_attempt("act_cheng", "户外跑 程启航", "FAILED", "报名人数已经满")
        reporter.record_attempt("act_wang", "户外跑 王彤帆", "SUCCESS", "报名成功")
        reporter.record_attempt("act_liao", "户外跑 廖华", "RETRYABLE", "提交异常(请求超时)，复核确认未报名")
        reporter.record_attempt("act_zhou", "户外跑 周天一", "SUCCESS", "报名成功")

        # 2. 注入真实 2 次复核明细
        reporter.record_verification("act_cheng", "户外跑 程启航", "RETRYABLE", "复核确认未报名")
        reporter.record_verification("act_cheng", "户外跑 程启航", "RETRYABLE", "复核确认未报名")

        # 3. 注入审计回放数据（模拟抢课循环结束时 replay 汇总并 set_audit_summary）
        events = AuditLogReader.read_events_from_file(self.event_file)
        audit_summary = AuditLogReader.replay(events, start_time="2026-11-08T13:58:00", end_time="2026-11-08T14:12:00")
        audit_summary["submission_started_count"] = 6
        audit_summary["submission_succeeded_count"] = 2
        audit_summary["submission_failed_count"] = 2
        audit_summary["submission_unknown_count"] = 2
        audit_summary["verification_started_count"] = 2
        audit_summary["verification_confirmed_unregistered_count"] = 2
        reporter.set_audit_summary(audit_summary)

        # 4. 服务端最终对账（落地 2 节：王彤帆、周天一，无未决）
        reconcile_report = {
            "success": True,
            "status": "SUCCESS",
            "run_id": "run_20261108_final",
            "complete": True,
            "total_registered": 2,
            "normal_registered": 2,
            "items": [
                {"id": "act_wang", "name": "户外跑 王彤帆", "isRegister": True, "startTime": 1731407700000},
                {"id": "act_zhou", "name": "户外跑 周天一", "isRegister": True, "startTime": 1731234900000}
            ]
        }
        reporter.set_reconciliation_result(
            reconciled_items=reconcile_report["items"],
            unresolved_count=0,
            normal_registered=2,
            max_normal=2,
            reconciliation_report=reconcile_report
        )

        # 5. 生成报告并断言
        sm = reporter.generate_summary()
        self.assertEqual(sm["infrastructure"]["http_failed"], 18)
        self.assertEqual(sm["infrastructure"]["outage_seconds"], 49.0)
        self.assertEqual(sm["attempts"]["total"], 6)
        self.assertEqual(len(sm["attempts"]["records"]), 6)
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 2)
        self.assertEqual(sm["reconciliation"]["status"], "SUCCESS")
        self.assertIn(sm["verdict"]["status"], (ReportStatus.SUCCESS, ReportStatus.PARTIAL_SUCCESS))

        # 6. 生成 Markdown 报告文本检查
        report_path = reporter.save_markdown_report(report_dir=self.logs_dir, base_name="2026-11-08-verified-report")
        with open(report_path, "r", encoding="utf-8") as f:
            md_text = f.read()

        # 严格检查关键章节呈现
        self.assertIn("- **提交尝试总数**：6", md_text)
        self.assertIn("- **明确成功 (Explicit Success)**：2", md_text)
        self.assertIn("- **请求异常总数**：18 次", md_text)
        self.assertIn("- **累计服务中断**：49.0 秒", md_text)
        self.assertIn("- **服务端确认报名成功**：2 节", md_text)
        self.assertIn("户外跑 王彤帆", md_text)
        self.assertIn("户外跑 周天一", md_text)
        self.assertIn("### 7.1 活动列表 (activities) 状态", md_text)
        self.assertIn("### 7.2 冲突检查 (checkConflict) 状态", md_text)

    # ---------------- 6. UNKNOWN 复核成功分支 ----------------
    def test_06_unknown_resolve_success_branch(self):
        """覆盖 UNKNOWN 复核确认已报名分支 (resolve_unknown_success / confirmed_registered)"""
        raw_events = [
            {"t": "2026-11-08T14:15:00", "event": "resolve_unknown_success", "aid": "act_test", "name": "测试活动"}
        ]
        tmp_file = os.path.join(self.logs_dir, "test_verify_succ.jsonl")
        with open(tmp_file, "w", encoding="utf-8") as f:
            for item in raw_events:
                f.write(json.dumps(item) + "\n")

        events = AuditLogReader.read_events_from_file(tmp_file)
        summary = AuditLogReader.replay(events)
        self.assertEqual(summary["verification_confirmed_registered_count"], 1)

    # ---------------- 7. UNKNOWN 仍未解决导致最终结论为 UNVERIFIED ----------------
    def test_07_unknown_remains_blocks_and_verdict_unverified(self):
        """覆盖 UNKNOWN 仍未解决时判定为 UNVERIFIED，杜绝误判 SUCCESS"""
        reporter = ResultReporter(run_date=datetime.date(2026, 11, 8), run_id="run_unverified_test")
        reporter.uncertain_count = 1
        reporter.unresolved_count = 1
        reporter.local_unknown_count = 1

        verdict_status, verdict_desc = reporter.compute_final_verdict()
        self.assertEqual(verdict_status, ReportStatus.UNVERIFIED)
        self.assertIn("未决", verdict_desc)

    # ---------------- 8. checkConflict 与 activities 失败隔离 ----------------
    def test_08_conflict_failures_independent_from_activities(self):
        """验证 checkConflict 失败独立统计，不污染 activities 的 http_requests_failed"""
        reporter = ResultReporter(run_date=datetime.date(2026, 11, 8), run_id="run_conflict_test")
        reporter.record_http_failure(500)
        reporter.record_conflict_failure(502)

        sm = reporter.generate_summary()
        self.assertEqual(sm["infrastructure"]["http_failed"], 1)
        self.assertEqual(sm["infrastructure"]["http_failures_breakdown"], {500: 1})
        self.assertEqual(sm["conflict_check"]["failures_breakdown"], {502: 1})


if __name__ == "__main__":
    unittest.main()
