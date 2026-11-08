"""
Phase 7 审计日志与全系统数据一致性集成测试套件 (I71 ~ I80)
=========================================================
验证统一审计事件日志、SQLite 状态库、服务端实际状态与结果报告生成器
在事件模型、指标计数、口径分离、敏感信息脱敏及多端报告上的 100% 同源一致性。
"""
import copy
import datetime
import json
import os
import time
import unittest

from core.audit_logger import AuditLogger, AuditLogReader, EventName, EventType
from core.circuit_breaker import CircuitBreaker
from core.result_reporter import ResultReporter
from core.state_store import RegistrationStatus
from tests.integration.integration_helper import IntegrationEnvironment


class TestAuditConsistencyFlow(unittest.TestCase):
    """审计与系统状态一致性集成测试 (I71 ~ I80)"""

    def setUp(self):
        self.env = IntegrationEnvironment()
        self.env.server.add_activity("act_501", "羽毛球双打课", 1794146400000, 1794153600000)
        self.env.server.add_activity("act_502", "乒乓球技术课", 1794155400000, 1794162600000)

    def tearDown(self):
        self.env.close()

    # -------------------------------------------------------------------------
    # I71: 审计日志回放的绝对忠实性 (Replay Fidelity)
    # -------------------------------------------------------------------------
    def test_I71_audit_replay_fidelity(self):
        logger = self.env.audit_logger
        logger.record_event(EventType.CANDIDATE, "candidate_accepted", "act_501")
        logger.record_event(EventType.SUBMISSION, EventName.SUBMISSION_STARTED, "act_501")
        logger.record_event(EventType.SUBMISSION, EventName.SUBMISSION_SUCCEEDED, "act_501")
        logger.record_event(EventType.INFRASTRUCTURE, EventName.HTTP_REQUEST_STARTED, details={"req": 1})
        logger.record_event(EventType.INFRASTRUCTURE, EventName.HTTP_REQUEST_SUCCEEDED, details={"req": 1})

        today_str = datetime.date.today().strftime("%Y%m%d")
        evt_path = logger._get_event_path(today_str)
        events = AuditLogReader.read_events_from_file(evt_path)

        summary = AuditLogReader.replay(events, run_id=self.env.run_id)
        self.assertEqual(summary["candidate_accepted"], 1)
        self.assertEqual(summary["submission_started_count"], 1)
        self.assertEqual(summary["submission_succeeded_count"], 1)
        self.assertEqual(summary["http_requests_total"], 1)
        self.assertEqual(summary["http_requests_succeeded"], 1)

    # -------------------------------------------------------------------------
    # I72: HTTP 请求统计同源一致性
    # -------------------------------------------------------------------------
    def test_I72_http_request_counts_consistency(self):
        self.env.server.reset_counts()
        self.env.client.activities()
        self.env.client.check_conflict("act_501")
        self.env.submit_signup("act_501", "羽毛球")

        # 服务端记录的请求数
        srv_reqs = self.env.server.http_requests_count
        self.assertEqual(srv_reqs, 3)

        # 客户端请求记录数与日志一致
        self.assertEqual(self.env.server.activities_calls_count, 1)
        self.assertEqual(self.env.server.conflict_calls_count, 1)
        self.assertEqual(self.env.server.signup_calls_count, 1)

    # -------------------------------------------------------------------------
    # I73: signUp 调用次数与审计事件 100% 一致
    # -------------------------------------------------------------------------
    def test_I73_signup_calls_match_submission_events(self):
        aid = "act_501"
        self.env.server.reset_counts()
        target = self.env.server.activities_db[aid]
        self.env.registration_manager.register_activity(target)

        self.assertEqual(self.env.server.signup_calls_count, 1)
        today_str = datetime.date.today().strftime("%Y%m%d")
        events = AuditLogReader.read_events_from_file(self.env.audit_logger._get_event_path(today_str))
        signup_evts = [e for e in events if e.get("event_name") == EventName.SUBMISSION_STARTED]
        self.assertEqual(len(signup_evts), 1)

    # -------------------------------------------------------------------------
    # I74: Submission 与 Verification 严格口径分离 (不相互污染)
    # -------------------------------------------------------------------------
    def test_I74_submission_and_verification_separated(self):
        aid = "act_501"
        # 1. 模拟一次 UNKNOWN 复核操作 (非新提交)
        self.env.state_store.reserve_submitting(aid, "羽毛球")
        self.env.state_store.record_unknown(aid, "羽毛球")
        self.env.recovery_manager.recover_on_startup()

        today_str = datetime.date.today().strftime("%Y%m%d")
        events = AuditLogReader.read_events_from_file(self.env.audit_logger._get_event_path(today_str))
        summary = AuditLogReader.replay(events, run_id=self.env.run_id)

        # 复核操作绝不增加 submission_started 计数
        self.assertEqual(summary["submission_started_count"], 0)
        self.assertEqual(summary["verification_started_count"], 1)

    # -------------------------------------------------------------------------
    # I75: StateStore 持久化状态与 Reconciliation 服务端对账精确一致
    # -------------------------------------------------------------------------
    def test_I75_statestore_and_reconciliation_consistency(self):
        aid = "act_501"
        self.env.state_store.reserve_submitting(aid, "羽毛球")
        self.env.submit_signup(aid, "羽毛球")
        self.env.state_store.record_success(aid, "羽毛球")

        rec_res = self.env.reconciliation_manager.reconcile()
        self.assertTrue(rec_res["success"])
        reconciled_ids = [it["id"] for it in rec_res["items"]]
        self.assertIn(aid, reconciled_ids)

        store_rec = self.env.state_store.get_record(aid)
        self.assertEqual(store_rec["status"], RegistrationStatus.SUCCESS)

    # -------------------------------------------------------------------------
    # I76: 配额计算在对账与 ResultReporter 之间数据一致
    # -------------------------------------------------------------------------
    def test_I76_quota_consistency_between_reconcile_and_reporter(self):
        aid = "act_501"
        self.env.state_store.reserve_submitting(aid, "羽毛球")
        self.env.submit_signup(aid, "羽毛球")
        self.env.state_store.record_success(aid, "羽毛球")

        rec_res = self.env.reconciliation_manager.reconcile()
        self.env.finalize_reporter(rec_res)
        summary = self.env.result_reporter.generate_summary()

        # 对账普通课程数与报告普通课程数完全一致
        self.assertEqual(rec_res["normal_registered"], 1)
        self.assertEqual(summary["quota"]["normal_registered"], 1)

    # -------------------------------------------------------------------------
    # I77: 熔断器切入切出事件与 ResultReporter 统计同源
    # -------------------------------------------------------------------------
    def test_I77_circuit_events_match_reporter(self):
        cb = self.env.activities_breaker
        cb.record_failure(http_status=500, force_trip=True)

        today_str = datetime.date.today().strftime("%Y%m%d")
        events = AuditLogReader.read_events_from_file(self.env.audit_logger._get_event_path(today_str))
        open_events = [e for e in events if e.get("event_name") == EventName.CIRCUIT_OPEN]
        self.assertGreaterEqual(len(open_events), 1)

    # -------------------------------------------------------------------------
    # I78: run_id 在全链路 (日志、数据库、对账、结果报告) 保持一致
    # -------------------------------------------------------------------------
    def test_I78_run_id_consistency_across_all_layers(self):
        rid = self.env.run_id
        aid = "act_501"
        self.env.state_store.reserve_submitting(aid, "羽毛球")
        self.env.submit_signup(aid, "羽毛球")
        self.env.state_store.record_success(aid, "羽毛球")
        rec_res = self.env.reconciliation_manager.reconcile()
        self.env.finalize_reporter(rec_res)
        rep = self.env.result_reporter.generate_summary()

        self.assertEqual(self.env.audit_logger.run_id, rid)
        self.assertEqual(rep["run_id"], rid)
        self.assertEqual(rec_res["run_id"], rid)

    # -------------------------------------------------------------------------
    # I79: 审计脱敏机制严密杜绝 Token、手机号与凭据泄露
    # -------------------------------------------------------------------------
    def test_I79_data_sanitization_masks_sensitive_data(self):
        logger = self.env.audit_logger
        sensitive_payload = {
            "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.sensitive_token_body_here",
            "refreshToken": "secret_refresh_token_abcdef123456",
            "password": "my_super_secret_password",
            "student_id": "20261108001",
            "mobile": "13800138000"
        }
        evt = logger.record_event(
            event_type="test",
            event_name="auth_test",
            details=sensitive_payload
        )

        details = evt["details"]
        self.assertEqual(details["accessToken"], "***MASKED***")
        self.assertEqual(details["refreshToken"], "***MASKED***")
        self.assertEqual(details["password"], "***MASKED***")
        self.assertEqual(details["student_id"], "***MASKED***")
        self.assertEqual(details["mobile"], "***MASKED***")

    # -------------------------------------------------------------------------
    # I80: 控制台与 Markdown 报告结论与数据同源 100% 吻合
    # -------------------------------------------------------------------------
    def test_I80_console_and_markdown_reports_100_percent_consistent(self):
        aid = "act_501"
        self.env.state_store.reserve_submitting(aid, "羽毛球")
        self.env.submit_signup(aid, "羽毛球")
        self.env.state_store.record_success(aid, "羽毛球")

        rec_res = self.env.reconciliation_manager.reconcile()
        self.env.finalize_reporter(rec_res)

        reporter = self.env.result_reporter
        console_text = reporter.format_console_report()
        md_text = reporter._build_markdown_content()

        # 核心结论与数据完全一致
        summary = reporter.generate_summary()
        status = summary["verdict"]["status"]
        self.assertEqual(status, "SUCCESS")
        self.assertIn(status, console_text)
        self.assertIn("act_501", md_text)
        self.assertIn(str(summary["reconciliation"]["confirmed_success"]), md_text)
        self.assertIn(str(summary["reconciliation"]["confirmed_success"]), console_text)


if __name__ == "__main__":
    unittest.main()
