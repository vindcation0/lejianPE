"""
Phase 3 统一审计日志与事件统计测试套件 (A01～A15)
==============================================
验证第四轮优化 Phase 3 要求的统一审计日志体系与事件统计模型：
- A01: 每次 HTTP 请求都有独立事件
- A02: 连续多次 HTTP 失败准确统计
- A03: Submission 与 Verification 完全分离
- A04: 规则拦截不增加 Submission
- A05: 一次 submit_once 对应一次提交事件
- A06: HTTP 失败事件与故障开始事件分离
- A07: 内部重试准确记录底层请求次数
- A08: 同一个 correlation_id 能关联完整业务过程
- A09: 不同 run_id 的事件不会混淆
- A10: JSONL 写入失败能够明确告警
- A11: Token 等敏感信息不会进入审计日志
- A12: 旧版日志格式仍然可以读取
- A13: 同一事件重复处理不会重复累计业务统计
- A14: 活动列表 complete=False 不会丢失 HTTP 失败统计
- A15: 历史抢课记录回放结果符合已确认的原始数据
"""
import collections
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from core.audit_logger import AuditLogger, AuditLogReader, EventName, EventType
from core.circuit_breaker import CircuitBreaker, CircuitState
from core.registration_manager import RegistrationManager
from core.request_manager import RequestManager, SubmitResult
from core.state_store import RegistrationStatus, StateStore
from lejian import ActivityList, Client, _request


class AuditLoggerTestSuite(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logs_dir = os.path.join(self.temp_dir.name, "logs")
        self.db_path = os.path.join(self.temp_dir.name, "test_audit.db")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.audit_logger = AuditLogger(self.logs_dir, run_id="run_test_001")
        self.state_store = StateStore(self.db_path)
        self.mock_client = MagicMock()
        self.req_mgr = RequestManager(self.mock_client, audit_logger=self.audit_logger)
        self.reg_mgr = RegistrationManager(self.state_store, self.req_mgr, self.audit_logger)

        self.fake_state = {
            "accessToken": "test_token_abc123",
            "refreshToken": "test_refresh_def456",
            "accessTokenExpires": int((datetime.datetime.now() + datetime.timedelta(hours=2)).timestamp() * 1000),
            "schoolId": "test_school_01",
            "semesterId": "test_semester_01"
        }

    def tearDown(self):
        self.state_store.close()
        self.temp_dir.cleanup()

    def _get_event_files(self):
        return [os.path.join(self.logs_dir, f) for f in os.listdir(self.logs_dir) if f.startswith("events-")]

    def _read_all_events(self):
        events = []
        for p in self._get_event_files():
            events.extend(AuditLogReader.read_events_from_file(p))
        return events

    # ---------------- A01: 每次 HTTP 请求都有独立事件 ----------------
    def test_A01_every_http_request_has_independent_event(self):
        """A01: 每次 HTTP 请求都有独立事件 (started + succeeded/failed, 唯一 event_id, 状态码, 耗时)"""
        c = Client(self.fake_state, audit_logger=self.audit_logger)

        # 模拟 1 次成功 (200) 和 1 次失败 (500)
        def fake_urlopen(req, timeout=15, context=None):
            if "fail" in req.full_url:
                fp = io.BytesIO(b'{"code": 500, "msg": "server error"}')
                raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, fp)
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b'{"code": 0, "data": {}}'
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            code1, _ = c.get("education/app/activity/successEndpoint")
            code2, _ = c.post("education/app/activity/failEndpoint", body={"id": "1"})

        self.assertEqual(code1, 200)
        self.assertEqual(code2, 500)

        events = self._read_all_events()
        # 2 次 HTTP 请求，应有 2 个 started, 1 个 succeeded, 1 个 failed
        started_events = [e for e in events if e.get("event_name") == EventName.HTTP_REQUEST_STARTED]
        succ_events = [e for e in events if e.get("event_name") == EventName.HTTP_REQUEST_SUCCEEDED]
        fail_events = [e for e in events if e.get("event_name") == EventName.HTTP_REQUEST_FAILED]

        self.assertEqual(len(started_events), 2)
        self.assertEqual(len(succ_events), 1)
        self.assertEqual(len(fail_events), 1)

        # 校验字段规范
        all_eids = [e["event_id"] for e in events]
        self.assertEqual(len(all_eids), len(set(all_eids)), "每个事件必须具有唯一 event_id")

        for e in events:
            self.assertEqual(e["schema_version"], 1)
            self.assertEqual(e["run_id"], "run_test_001")
            self.assertIn("timestamp", e)
            self.assertIn("duration_ms", e)

        self.assertEqual(succ_events[0]["http_status"], 200)
        self.assertEqual(fail_events[0]["http_status"], 500)

    # ---------------- A02: 连续多次 HTTP 失败准确统计 ----------------
    def test_A02_consecutive_http_failures_counted_accurately(self):
        """A02: 连续多次 HTTP 失败准确统计 (3次500, 2次超时-1)"""
        c = Client(self.fake_state, audit_logger=self.audit_logger)

        fail_seq = [
            urllib.error.HTTPError("url", 500, "Internal Server Error", {}, io.BytesIO(b'500')),
            urllib.error.HTTPError("url", 500, "Internal Server Error", {}, io.BytesIO(b'500')),
            urllib.error.HTTPError("url", 500, "Internal Server Error", {}, io.BytesIO(b'500')),
            urllib.error.URLError("timed out"),
            urllib.error.URLError("timed out"),
        ]

        with patch("urllib.request.urlopen", side_effect=fail_seq):
            for _ in range(5):
                c.get("education/app/activity/fail")

        events = self._read_all_events()
        res = AuditLogReader.replay(events)

        self.assertEqual(res["http_requests_failed"], 5)
        self.assertEqual(res["http_failures_by_code"], {500: 3, -1: 2})

    # ---------------- A03: Submission 与 Verification 完全分离 ----------------
    def test_A03_submission_and_verification_strictly_separated(self):
        """A03: Submission 与 Verification 完全分离，复核绝不计入 Submission"""
        # 1. 发起一次真实提交
        self.mock_client.call.return_value = (200, {"code": 0, "data": {"success": True}})
        self.reg_mgr.register_activity(item={"id": "1001", "name": "户外跑", "startTime": 1600000000000})

        # 2. 发起两次状态复核
        act_list = ActivityList([{"id": "1001", "isRegister": True}], complete=True, total=1)
        self.mock_client.activities.return_value = act_list
        self.req_mgr.verify_registration("1001")
        self.req_mgr.verify_registration("1001")

        events = self._read_all_events()
        sub_events = [e for e in events if e.get("event_type") == EventType.SUBMISSION]
        ver_events = [e for e in events if e.get("event_type") == EventType.VERIFICATION]

        self.assertTrue(len(sub_events) >= 1)
        self.assertTrue(len(ver_events) >= 2)

        res = AuditLogReader.replay(events)
        self.assertEqual(res["submission_started_count"], 1, "Submission 数量必须恰好为 1，复核不可增加提交计数")
        self.assertEqual(res["verification_started_count"], 2, "Verification 独立统计，恰好为 2")

    # ---------------- A04: 规则拦截不增加 Submission ----------------
    def test_A04_rule_rejection_does_not_increment_submission(self):
        """A04: 规则拦截产生 candidate_rejected，绝不发起 submit_once，不增加 Submission 计数"""
        def reject_validator(item):
            return False, "活动时段与已有课表冲突"

        ok, status, reason, _ = self.reg_mgr.register_activity(
            item={"id": "2001", "name": "户外跑", "startTime": 1600000000000},
            validate_rules_cb=reject_validator
        )

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.FAILED)
        self.assertIn("规则校验未通过", reason)

        # 确认未调用底层网络提交
        self.mock_client.call.assert_not_called()

        events = self._read_all_events()
        res = AuditLogReader.replay(events)

        self.assertEqual(res["candidate_rejected"], 1)
        self.assertEqual(res["candidate_accepted"], 0)
        self.assertEqual(res["submission_started_count"], 0, "规则拦截严禁产生 Submission 事件")

    # ---------------- A05: 一次 submit_once 对应一次提交事件 ----------------
    def test_A05_single_submit_once_corresponds_to_single_submission_event(self):
        """A05: 一次 submit_once 严格对应一次提交事件，禁止自动重试"""
        self.mock_client.call.return_value = (200, {"code": 0, "data": {"success": True}})

        ok, status, msg, _ = self.reg_mgr.register_activity(
            item={"id": "3001", "name": "太极拳", "startTime": 1600000000000}
        )

        self.assertTrue(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)

        events = self._read_all_events()
        sub_starts = [e for e in events if e.get("event_name") == EventName.SUBMISSION_STARTED]
        sub_succs = [e for e in events if e.get("event_name") == EventName.SUBMISSION_SUCCEEDED]

        self.assertEqual(len(sub_starts), 1)
        self.assertEqual(len(sub_succs), 1)

        res = AuditLogReader.replay(events)
        self.assertEqual(res["submission_started_count"], 1)
        self.assertEqual(res["submission_succeeded_count"], 1)

    # ---------------- A06: HTTP 失败事件与故障开始事件分离 ----------------
    def test_A06_http_failure_events_separated_from_circuit_outage_events(self):
        """A06: 连续多次 HTTP 失败期间，每次请求失败独立记录，但 circuit_open 故障开始事件不重复创建"""
        cb = CircuitBreaker(failure_threshold=3, base_cooldown=10.0, audit_logger=self.audit_logger)

        # 连续记录 5 次 HTTP 500 故障
        for _ in range(5):
            cb.record_failure(http_status=500)

        events = self._read_all_events()
        trip_events = [e for e in events if e.get("event_name") == EventName.CIRCUIT_OPEN]

        # 达到阈值 3 时触发进入 OPEN 一次，第 4、5 次失败处于 OPEN 期间，不可重复触发 circuit_open
        self.assertEqual(len(trip_events), 1)
        self.assertEqual(cb.state, CircuitState.OPEN)
        self.assertEqual(cb.failure_count, 5)

        res = AuditLogReader.replay(events)
        self.assertEqual(res["circuit_trips"], 1)

    # ---------------- A07: 内部重试准确记录底层请求次数 ----------------
    def test_A07_internal_retries_accurately_record_underlying_requests(self):
        """A07: 内部重试准确记录每次底层 HTTP 请求 (max_retries=2 导致 3 次底层请求)"""
        c = Client(self.fake_state, audit_logger=self.audit_logger)

        call_idx = 0
        def fake_urlopen(req, timeout=15, context=None):
            nonlocal call_idx
            call_idx += 1
            if call_idx < 3:
                # 前 2 次底层请求返回 HTTP 500
                fp = io.BytesIO(b'{"code": 500}')
                raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, fp)
            # 第 3 次底层请求成功返回
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = json.dumps({"code": 0, "data": {"items": [], "total": 0}}).encode("utf-8")
            resp.__enter__.return_value = resp
            return resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res_items = c.activities(page_size=200, req_timeout=5, max_retries=2)

        self.assertTrue(res_items.complete)
        self.assertEqual(call_idx, 3)

        events = self._read_all_events()
        res = AuditLogReader.replay(events)

        self.assertEqual(res["http_requests_total"], 3, "底层 HTTP 请求总数必须为 3")
        self.assertEqual(res["http_requests_failed"], 2, "底层失败请求必须记录为 2")
        self.assertEqual(res["http_requests_succeeded"], 1, "底层成功请求必须记录为 1")

    # ---------------- A08: 同一个 correlation_id 能关联完整业务过程 ----------------
    def test_A08_same_correlation_id_associates_complete_lifecycle(self):
        """A08: 同一个 correlation_id 串联 发现 -> 评估 -> 提交 -> 未决 -> 复核 全过程"""
        # 模拟提交超时 (UNKNOWN)，随后只读复核确认已成功落库
        def fake_call(method, path, body=None, timeout=15):
            if "signUp" in path:
                return -1, json.dumps({"_raw": "Timeout"})
            return 200, json.dumps({"code": 0})

        self.mock_client.call.side_effect = fake_call
        self.mock_client.activities.return_value = ActivityList(
            [{"id": "4001", "name": "羽毛球", "isRegister": True}],
            complete=True, total=1
        )

        ok, status, msg, _ = self.reg_mgr.register_activity(
            item={"id": "4001", "name": "羽毛球", "startTime": 1600000000000}
        )

        self.assertTrue(ok)
        self.assertEqual(status, RegistrationStatus.SUCCESS)

        events = self._read_all_events()
        act_events = [e for e in events if e.get("activity_id") == "4001"]

        self.assertTrue(len(act_events) >= 4)
        cids = {e.get("correlation_id") for e in act_events}
        self.assertEqual(len(cids), 1, f"所有生命周期事件必须共享唯一 correlation_id，实际为: {cids}")

    # ---------------- A09: 不同 run_id 的事件不会混淆 ----------------
    def test_A09_distinct_run_ids_never_collide(self):
        """A09: 不同 run_id 的事件在回放与读取时严格隔离不混淆"""
        logger_a = AuditLogger(self.logs_dir, run_id="run_20261108_001")
        logger_b = AuditLogger(self.logs_dir, run_id="run_20261108_002")

        logger_a.record_event(event_type=EventType.SUBMISSION, event_name=EventName.SUBMISSION_STARTED)
        logger_a.record_event(event_type=EventType.SUBMISSION, event_name=EventName.SUBMISSION_SUCCEEDED)

        logger_b.record_event(event_type=EventType.SUBMISSION, event_name=EventName.SUBMISSION_STARTED)
        logger_b.record_event(event_type=EventType.SUBMISSION, event_name=EventName.SUBMISSION_FAILED)
        logger_b.record_event(event_type=EventType.INFRASTRUCTURE, event_name=EventName.HTTP_REQUEST_FAILED, http_status=500)

        events = self._read_all_events()

        # 指定 run_id 回放
        rep_a = AuditLogReader.replay(events, run_id="run_20261108_001")
        self.assertEqual(rep_a["total_events"], 2)
        self.assertEqual(rep_a["submission_succeeded_count"], 1)
        self.assertEqual(rep_a["submission_failed_count"], 0)

        rep_b = AuditLogReader.replay(events, run_id="run_20261108_002")
        self.assertEqual(rep_b["total_events"], 3)
        self.assertEqual(rep_b["submission_failed_count"], 1)
        self.assertEqual(rep_b["http_requests_failed"], 1)

        # 全局回放
        rep_all = AuditLogReader.replay(events)
        self.assertEqual(rep_all["total_events"], 5)
        self.assertEqual(rep_all["runs_count"], 2)

    # ---------------- A10: JSONL 写入失败能够明确告警 ----------------
    def test_A10_jsonl_write_failure_warns_cleanly_without_crashing(self):
        """A10: JSONL 写入失败优雅处理：累加错误计数、输出警告日志，绝不崩溃打断主业务"""
        logger = AuditLogger(self.logs_dir, run_id="run_write_fail")

        err_buf = io.StringIO()
        with patch("sys.stderr", err_buf):
            with patch("builtins.open", side_effect=OSError("Disk full or permission denied")):
                rec = logger.record_event(
                    event_type=EventType.SUBMISSION,
                    event_name=EventName.SUBMISSION_STARTED,
                    activity_id="5001"
                )

        self.assertIsNotNone(rec)
        self.assertEqual(logger.write_error_count, 1)
        self.assertIn("写入审计事件日志失败", err_buf.getvalue())

    # ---------------- A11: Token 等敏感信息不会进入审计日志 ----------------
    def test_A11_sensitive_tokens_masked_in_audit_log(self):
        """A11: 敏感信息脱敏：Token、密码、手机号、学号、凭据等严禁明文入库"""
        self.audit_logger.record_event(
            event_type=EventType.INFRASTRUCTURE,
            event_name=EventName.HTTP_REQUEST_FAILED,
            reason="Request failed with Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret",
            details={
                "accessToken": "secret_access_token_999",
                "refreshToken": "secret_refresh_token_888",
                "password": "MySecretPassword!",
                "mobile": "13800138000",
                "studentId": "20230001",
                "signDigital": "sensitive_sig_12345"
            }
        )

        files = self._get_event_files()
        with open(files[0], "r", encoding="utf-8") as f:
            raw_text = f.read()

        self.assertNotIn("secret_access_token_999", raw_text)
        self.assertNotIn("secret_refresh_token_888", raw_text)
        self.assertNotIn("MySecretPassword!", raw_text)
        self.assertNotIn("13800138000", raw_text)
        self.assertNotIn("20230001", raw_text)
        self.assertNotIn("sensitive_sig_12345", raw_text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", raw_text)
        self.assertIn("***MASKED***", raw_text)

    # ---------------- A12: 旧版日志格式仍然可以读取 ----------------
    def test_A12_legacy_log_format_backward_compatible(self):
        """A12: 旧版 JSONL 日志格式 (如 list_fail, list_ok) 仍可解析与回放"""
        legacy_file = os.path.join(self.logs_dir, "events-20261025.jsonl")
        legacy_lines = [
            json.dumps({"t": "2026-10-25T13:59:58", "event": "list_fail", "http": -1}),
            json.dumps({"t": "2026-10-25T14:00:20", "event": "list_fail", "http": 500}),
            json.dumps({"t": "2026-10-25T14:01:00", "event": "list_ok", "outage_sec": 62.0}),
        ]
        with open(legacy_file, "w", encoding="utf-8") as f:
            for l in legacy_lines:
                f.write(l + "\n")

        events = AuditLogReader.read_events_from_file(legacy_file)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["schema_version"], 0)

        res = AuditLogReader.replay(events)
        self.assertEqual(res["http_requests_failed"], 2)
        self.assertEqual(res["http_failures_by_code"], {-1: 1, 500: 1})
        self.assertEqual(res["circuit_closed_count"], 1)

    # ---------------- A13: 同一事件重复处理不会重复累计业务统计 ----------------
    def test_A13_replay_deduplicates_duplicate_events(self):
        """A13: 幂等回放：相同 event_id 的重复事件自动去重，绝不重复统计"""
        evt = {
            "schema_version": 1,
            "event_id": "evt_duplicate_id_001",
            "run_id": "run_test_dedup",
            "correlation_id": "corr_001",
            "timestamp": "2026-11-08T14:11:00.000",
            "event_type": EventType.SUBMISSION,
            "event_name": EventName.SUBMISSION_STARTED,
            "activity_id": "6001",
            "status": "SUBMITTING",
            "http_status": None,
            "duration_ms": 100.0,
            "source": "test"
        }

        # 包含 3 条完全重复 event_id 的列表
        duplicate_stream = [evt, evt, evt]

        res = AuditLogReader.replay(duplicate_stream)
        self.assertEqual(res["total_events"], 1)
        self.assertEqual(res["distinct_event_ids"], 1)
        self.assertEqual(res["submission_started_count"], 1, "去重后提交数必须为 1 而非 3")

    # ---------------- A14: 活动列表 complete=False 不会丢失 HTTP 失败统计 ----------------
    def test_A14_activity_list_incomplete_preserves_http_failure_stats(self):
        """A14: 活动列表由于 HTTP 故障返回 complete=False 时，底层 HTTP 失败统计绝对不丢失"""
        c = Client(self.fake_state, audit_logger=self.audit_logger)

        # 模拟 POST 请求失败返回 HTTP 500
        def fake_urlopen(req, timeout=15, context=None):
            fp = io.BytesIO(b'{"code": 500, "msg": "Internal Error"}')
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", {}, fp)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            items = c.activities(page_size=200, req_timeout=5, max_retries=0)

        self.assertFalse(items.complete)
        self.assertEqual(items.http_code, 500)

        events = self._read_all_events()
        res = AuditLogReader.replay(events)

        self.assertEqual(res["http_requests_failed"], 1)
        self.assertEqual(res["http_failures_by_code"], {500: 1})

    # ---------------- A15: 历史抢课记录回放结果符合已确认的原始数据 ----------------
    def test_A15_historical_grab_log_replay_matches_ground_truth(self):
        """A15: 2026-11-08 历史抢课记录回放精准复现已确认的真实基准 (18次列表失败, 6次真实提交, 2次复核屏障)"""
        # 构建基于 2026-11-08 真实脱敏基准测试夹具 (Fixture)
        fixture_events = []

        # 1. 18 次活动列表请求失败 (-1: 8, 500: 8, 502: 1, 503: 1)
        failure_codes = [-1] * 8 + [500] * 8 + [502] * 1 + [503] * 1
        for idx, code in enumerate(failure_codes):
            fixture_events.append({
                "schema_version": 1,
                "event_id": f"evt_hist_http_fail_{idx:03d}",
                "run_id": "run_20261108_real",
                "correlation_id": f"corr_poll_{idx}",
                "timestamp": f"2026-11-08T13:58:{idx:02d}.000",
                "event_type": EventType.INFRASTRUCTURE,
                "event_name": EventName.HTTP_REQUEST_FAILED,
                "http_status": code,
                "duration_ms": 500.0,
                "source": "client_http"
            })

        # 2. 7 次列表恢复 / 熔断恢复
        for idx in range(7):
            fixture_events.append({
                "schema_version": 1,
                "event_id": f"evt_hist_rec_{idx:03d}",
                "run_id": "run_20261108_real",
                "timestamp": f"2026-11-08T14:09:{idx:02d}.000",
                "event_type": EventType.INFRASTRUCTURE,
                "event_name": EventName.CIRCUIT_CLOSED,
                "source": "circuit_breaker"
            })

        # 3. 阶段一：6 次 checkConflict 规则拦截 (不产生 Submission)
        for idx in range(6):
            fixture_events.append({
                "schema_version": 1,
                "event_id": f"evt_hist_cand_rej_{idx:03d}",
                "run_id": "run_20261108_real",
                "correlation_id": f"corr_check_conflict_{idx}",
                "timestamp": f"2026-11-08T14:09:{20+idx:02d}.000",
                "event_type": EventType.CANDIDATE,
                "event_name": EventName.CANDIDATE_REJECTED,
                "reason": "规则校验未通过: 服务端冲突检查接口异常",
                "source": "registration_manager"
            })

        # 4. 阶段二：6 次真实报名提交 (真实 submit_once 调用)
        submissions_data = [
            ("act_cheng_01", "户外跑 程启航", EventName.SUBMISSION_UNKNOWN, -1, "UNKNOWN"),
            ("act_cheng_02", "户外跑 程启航", EventName.SUBMISSION_UNKNOWN, 500, "UNKNOWN"),
            ("act_cheng_03", "户外跑 程启航", EventName.SUBMISSION_FAILED, 200, "FAILED"),
            ("act_wang_01", "户外跑 王彤帆", EventName.SUBMISSION_SUCCEEDED, 200, "SUCCESS"),
            ("act_liao_01", "户外跑 廖华", EventName.SUBMISSION_UNKNOWN, -1, "UNKNOWN"),
            ("act_zhou_01", "户外跑 周天一", EventName.SUBMISSION_SUCCEEDED, 200, "SUCCESS")
        ]

        for aid, name, end_evt, code, status_str in submissions_data:
            cid = f"corr_sub_{aid}"
            # 提交开始
            fixture_events.append({
                "schema_version": 1,
                "event_id": f"evt_sub_start_{aid}",
                "run_id": "run_20261108_real",
                "correlation_id": cid,
                "timestamp": "2026-11-08T14:11:00.000",
                "event_type": EventType.SUBMISSION,
                "event_name": EventName.SUBMISSION_STARTED,
                "activity_id": aid,
                "status": "SUBMITTING",
                "source": "registration_manager"
            })
            # 提交响应
            fixture_events.append({
                "schema_version": 1,
                "event_id": f"evt_sub_resp_{aid}",
                "run_id": "run_20261108_real",
                "correlation_id": cid,
                "timestamp": "2026-11-08T14:11:01.000",
                "event_type": EventType.SUBMISSION,
                "event_name": end_evt,
                "activity_id": aid,
                "status": status_str,
                "http_status": code,
                "source": "registration_manager"
            })

        # 5. 2 次 UNKNOWN 安全屏障复核 (独立 Verification 事件，绝不混淆为 Submission)
        for idx in range(2):
            fixture_events.append({
                "schema_version": 1,
                "event_id": f"evt_ver_start_{idx}",
                "run_id": "run_20261108_real",
                "correlation_id": f"corr_ver_{idx}",
                "timestamp": f"2026-11-08T14:11:{10+idx:02d}.000",
                "event_type": EventType.VERIFICATION,
                "event_name": EventName.VERIFICATION_STARTED,
                "activity_id": "act_cheng_01",
                "source": "request_manager"
            })
            fixture_events.append({
                "schema_version": 1,
                "event_id": f"evt_ver_end_{idx}",
                "run_id": "run_20261108_real",
                "correlation_id": f"corr_ver_{idx}",
                "timestamp": f"2026-11-08T14:11:{12+idx:02d}.000",
                "event_type": EventType.VERIFICATION,
                "event_name": EventName.VERIFICATION_CONFIRMED_UNREGISTERED,
                "activity_id": "act_cheng_01",
                "status": "RETRYABLE",
                "source": "request_manager"
            })

        # 执行回放
        res = AuditLogReader.replay(fixture_events)

        # 严格断言与历史 Ground Truth 100% 对齐
        self.assertEqual(res["http_requests_failed"], 18, "历史列表请求失败必须精确为 18 次")
        self.assertEqual(res["http_failures_by_code"], {-1: 8, 500: 8, 502: 1, 503: 1})
        self.assertEqual(res["candidate_rejected"], 6, "冲突检查拦截必须精确为 6 次")
        self.assertEqual(res["submission_started_count"], 6, "真实报名提交必须精确为 6 次 (修正旧报告多算的2次屏障)")
        self.assertEqual(res["submission_succeeded_count"], 2, "最终确认成功报上 2 节")
        self.assertEqual(res["submission_failed_count"], 1, "程启航满员失败 1 次")
        self.assertEqual(res["submission_unknown_count"], 3, "发生 UNKNOWN 3 次")
        self.assertEqual(res["verification_started_count"], 2, "安全屏障复核精确为 2 次，独立统计")


if __name__ == "__main__":
    unittest.main()
