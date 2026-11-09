"""
Phase 8: 2026-11-08 真实事件回放与审计报告数据闭环测试套件
==============================================================
基于 2026-11-08 抢课实战脱敏事件序列，验证数据闭环与报告口径修复：
1. 连续活动列表失败与多种 HTTP 状态码 (-1: 8次, 500: 8次, 502: 1次, 503: 1次，合计 18 次)。
2. 多次服务恢复与累计中断时长 (7 次恢复，累计 49.0 秒)。
3. UNKNOWN 产生、复核未报名与复核成功分支。
4. 第二实例 6 次提交与 3 次屏障复核分离（2 次确认未报名，1 次未确认）。
5. 从历史报告脱敏提取的 2 条报名记录，经 Mock 列表重新对账。
6. 原始事件去重幂等性 (Seen Event IDs)。
7. 审计事实单向优先性 (set_circuit_metrics 绝不覆盖 AuditLogReader 的真实统计)。
8. checkConflict 失败与 activities 列表失败独立统计。
"""
import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core.audit_logger import AuditLogger, AuditLogReader, EventName, EventType
from core.circuit_breaker import CircuitBreaker
from core.result_reporter import ReportStatus, ResultReporter
from core.reconciliation import ReconciliationManager
from core.registration_manager import RegistrationManager
from core.request_manager import RequestManager
from core.state_store import StateStore
from lejian import ActivityList, Client

# 只读取版本库中的脱敏样本，不依赖本机生产日志或配置。
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "replay_20261108.json").read_text(encoding="utf-8"))
RAW_EVENTS_20261108 = FIXTURE["events"]


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
        """兼容接口将旧调用中误记为提交的复核分流到独立明细。"""
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
        """从历史事件、提交记录和脱敏对账证据生成报告，不注入计数。"""
        second_start = FIXTURE["provenance"]["second_instance_started_at"]
        start, end = FIXTURE["provenance"]["event_window"]
        events = AuditLogReader.read_events_from_file(self.event_file)
        # 基础设施统计使用完整窗口，复核与提交只取第二实例。
        report_events = [
            event for event in events if event["event_type"] == EventType.INFRASTRUCTURE
            or event["timestamp"] >= second_start
        ]
        request_file = Path(self.logs_dir) / "requests-20261108.jsonl"
        request_file.write_text(
            "".join(json.dumps(row) + "\n" for row in FIXTURE["requests"]), encoding="utf-8"
        )
        report_events.extend(
            event for event in AuditLogReader.read_events_from_file(str(request_file))
            if event["timestamp"] >= second_start
        )

        logger = AuditLogger(self.logs_dir, run_id="run_20261108_fixture")
        reporter = ResultReporter(run_date=datetime.date(2026, 11, 8), run_id=logger.run_id)
        reporter.set_audit_summary(AuditLogReader.replay(
            report_events, run_id="legacy_run", start_time=start, end_time=end, log_dir=self.logs_dir
        ))

        # 用历史报告中的确认清单模拟只读列表响应。没有历史原始服务端响应。
        client = Mock()
        client.activities.return_value = ActivityList(FIXTURE["registered_items"], complete=True)
        result = ReconciliationManager(
            client, {"max_normal": 2, "max_per_day": 1}, {},
            lambda date, _: date.isocalendar().week, audit_logger=logger
        ).reconcile()
        reporter.set_reconciliation_result(
            reconciled_items=result["items"], unresolved_count=FIXTURE["unresolved_count_at_reconciliation"],
            normal_registered=result["normal_registered"], max_normal=2, reconciliation_report=result
        )
        client.activities.assert_called_once()

        sm = reporter.generate_summary()
        self.assertEqual(sm["infrastructure"]["http_failed"], 18)
        self.assertEqual(sm["infrastructure"]["outage_seconds"], 49.0)
        self.assertEqual(sm["attempts"]["total"], 6)
        self.assertEqual(len(sm["attempts"]["records"]), 6)
        self.assertEqual(sm["attempts"]["explicit_success"], 2)
        self.assertEqual(sm["verification"]["total"], 3)
        self.assertEqual(sm["verification"]["confirmed_unregistered"], 2)
        self.assertEqual(sm["verification"]["remains_unknown"], 1)
        self.assertEqual(len(sm["verification"]["records"]), 3)
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 2)
        self.assertEqual(sm["reconciliation"]["status"], "SUCCESS")
        self.assertIn(sm["verdict"]["status"], (ReportStatus.SUCCESS, ReportStatus.PARTIAL_SUCCESS))

        report_path = reporter.save_markdown_report(report_dir=self.logs_dir, base_name="2026-11-08-verified-report")
        md_text = Path(report_path).read_text(encoding="utf-8")
        for text in ("提交尝试总数**：6", "请求异常总数**：18 次",
                     "累计服务中断**：49.0 秒", "服务端确认报名成功**：2 节",
                     "### 7.1 活动列表 (activities) 状态", "### 7.2 冲突检查 (checkConflict) 状态"):
            self.assertIn(text, md_text)

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


    def test_09_rule_rejection_does_not_enter_audit_attempt_details(self):
        logger = AuditLogger(self.logs_dir)
        request_manager = Mock()
        request_manager.submit_once.side_effect = AssertionError("Submission is forbidden in this test")
        store = StateStore(":memory:")
        self.addCleanup(store.close)
        manager = RegistrationManager(store, request_manager, logger)
        manager.register_activity({"id": "blocked", "name": "blocked"}, lambda _: (False, "拒绝"))
        request_manager.submit_once.assert_not_called()
        events = AuditLogReader.read_events_from_file(logger._get_event_path(f"{datetime.date.today():%Y%m%d}"))
        reporter = ResultReporter()
        reporter.set_audit_summary(AuditLogReader.replay(events, run_id=logger.run_id))
        attempts = reporter.generate_summary()["attempts"]
        self.assertEqual((attempts["total"], attempts["records"]), (0, []))

    def test_10_request_pairing_and_activity_failure_breakdown(self):
        path = "education/app/activity/getActivityList"
        rows = [
            ("start", "http_request_started", "other", "checkConflict", None),
            ("other", "http_request_failed", "other", "checkConflict", 502),
            ("fallback", "list_fault_started", "missing", path, 503),
            ("paired", "list_fault_started", "list", path, 500),
            ("transport", "http_request_failed", "list", path, 500),
        ]
        events = [
            {"event_id": eid, "run_id": "r", "event_type": EventType.INFRASTRUCTURE,
             "event_name": event, "request_id": request_id, "details": {"path": url}, "http_status": code}
            for eid, event, request_id, url, code in rows
        ]
        events.append({
            "event_id": "conflict", "run_id": "r", "event_type": EventType.CONFLICT,
            "event_name": EventName.CONFLICT_CHECK_FAILED, "http_status": 502,
        })
        for sequence in (events, list(reversed(events))):
            summary = AuditLogReader.replay(sequence)
            reporter = ResultReporter()
            reporter.set_audit_summary(summary)
            reporter.set_audit_summary(summary)
            result = reporter.generate_summary()
            self.assertEqual(summary["http_requests_failed"], 3)
            self.assertEqual(result["infrastructure"]["http_failures_breakdown"], {500: 1, 503: 1})
            self.assertEqual(result["conflict_check"]["failures_breakdown"], {502: 1})
            reporter.set_audit_summary(AuditLogReader.replay([]))
            self.assertEqual(reporter.generate_summary()["conflict_check"]["failures_breakdown"], {})

    def test_11_zero_outage_remains_authoritative_and_missing_can_fall_back(self):
        record = {"event_type": EventType.INFRASTRUCTURE, "event_name": "list_ok",
                  "details": {"outage_sec": 0}, "duration_ms": 10000}
        reporter = ResultReporter()
        reporter.set_audit_summary(AuditLogReader.replay([record]))
        reporter.set_circuit_metrics(9, 1)
        self.assertEqual((reporter.outage_seconds, reporter.breaker_trips), (0.0, 0))
        reporter.set_audit_summary(AuditLogReader.replay([]))
        reporter.set_circuit_metrics(9, 1)
        self.assertEqual(reporter.outage_seconds, 9.0)

    def test_12_legacy_dedup_and_explicit_restart_scope(self):
        raw = json.dumps({"event": "list_fail", "http": 500, "t": "2026-11-08T14:05:00"})
        first = AuditLogReader.parse_line(raw, line_no=0)
        duplicate = AuditLogReader.parse_line(raw, line_no=2)
        previous = dict(first, run_id="previous", event_id="previous_id")
        current = dict(first, run_id="current", event_id="current_id")
        foreign = dict(current, run_id="other")
        rows = [foreign, previous, current, first, duplicate]
        self.assertEqual(AuditLogReader.replay(rows, run_id="current")["http_requests_failed"], 1)
        self.assertEqual(AuditLogReader.replay(rows, run_ids={"previous", "current"})["http_requests_failed"], 2)
        self.assertEqual(AuditLogReader.replay(rows, run_id="legacy_run")["http_requests_failed"], 1)
        self.assertEqual(AuditLogReader.replay(rows, start_time="2026-11-08T14:06:00")["total_events"], 0)

    def test_13_unknown_barrier_records_each_read_only_lookup_once(self):
        logger = AuditLogger(self.logs_dir)
        client = Mock()
        client.activities.return_value = ActivityList([{"id": "pending", "isRegister": False}], complete=True)
        store = StateStore(":memory:")
        self.addCleanup(store.close)
        store.reserve_submitting("pending", "pending")
        store.record_unknown("pending", "pending")
        manager = RegistrationManager(store, RequestManager(client, audit_logger=logger), logger)
        manager.resolve_unknown("pending", "pending")
        client.call.assert_not_called()
        events = AuditLogReader.read_events_from_file(logger._get_event_path(f"{datetime.date.today():%Y%m%d}"))
        summary = AuditLogReader.replay(events, run_id=logger.run_id)
        self.assertEqual(summary["submission_started_count"], 0)
        self.assertEqual(summary["verification_started_count"], 1)
        self.assertEqual(summary["verification_confirmed_unregistered_count"], 1)
        self.assertEqual(summary["verification_records"][0]["status"], "RETRYABLE")

    def test_14_changing_failure_code_keeps_first_outage_start(self):
        logger = Mock()
        client = Client({}, audit_logger=logger)
        start = datetime.datetime(2026, 11, 8, 14, 0, 0)
        with patch("lejian.log"), patch("lejian.datetime.datetime", wraps=datetime.datetime) as clock:
            clock.now.return_value = start
            client._note_fail(500)
            clock.now.return_value = start + datetime.timedelta(seconds=10)
            client._note_fail(502)
            clock.now.return_value = start + datetime.timedelta(seconds=20)
            client._note_ok()
        self.assertEqual(logger.record_event.call_args.kwargs["details"]["outage_sec"], 20)


    def test_15_client_list_failure_uses_transport_request_id(self):
        logger = AuditLogger(self.logs_dir)
        client = Client({"accessToken": "offline-test", "schoolId": "test", "semesterId": "test"},
                        audit_logger=logger)
        with patch.object(client, "ensure_token"), patch("lejian.log"), patch(
            "lejian.urllib.request.urlopen", side_effect=OSError("simulated offline failure")
        ) as urlopen:
            result = client.activities()
        urlopen.assert_called_once()
        self.assertFalse(result.complete)
        events = AuditLogReader.read_events_from_file(logger._get_event_path(f"{datetime.date.today():%Y%m%d}"))
        ids = {event["event_name"]: event["request_id"] for event in events}
        self.assertTrue(ids["list_fault_started"])
        self.assertEqual(ids["list_fault_started"], ids[EventName.HTTP_REQUEST_FAILED])
        self.assertEqual(AuditLogReader.replay(events)["activities_requests_failed"], 1)


if __name__ == "__main__":
    unittest.main()
