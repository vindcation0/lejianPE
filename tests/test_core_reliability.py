"""
核心业务可靠性专项测试套件 (Core Reliability Test Suite)
======================================================
覆盖《lejianPE 下一阶段核心业务可靠性优化执行计划》：
1. Token 自动刷新与持久化可靠性 (TK01～TK06)
2. 课表冲突判断与提交前三态校验 (CF01～CF05)
3. 抢课状态机异常边界与 UNKNOWN 屏障 (SM01～SM02)
4. 服务端逐活动对账与历史口径隔离 (RC01～RC03)
"""
import copy
import datetime
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import lejian
from core.audit_logger import AuditLogger
from core.reconciliation import ReconciliationManager
from core.registration_manager import RegistrationManager
from core.request_manager import RequestManager, SubmitResult
from core.result_reporter import ResultReporter
from core.state_store import RegistrationStatus, StateStore
from tests.network_guard import install_network_guard
from timetable.models import TimeSlot, Timetable


class CoreReliabilityTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_network_guard()

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_core_rel_")
        self.state_path = os.path.join(self.test_dir, "state.json")
        self.db_path = os.path.join(self.test_dir, "test.db")
        self.state_store = StateStore(self.db_path)
        self.audit_logger = AuditLogger(os.path.join(self.test_dir, "audit.log"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # Phase 2: Token 自动刷新可靠性 (TK01 ~ TK06)
    # =========================================================================

    def test_TK01_refresh_missing_access_token_raises_and_preserves(self):
        """TK01: Token 刷新响应中缺失 accessToken 时抛出异常，内存与磁盘均不被篡改"""
        init_state = {
            "accessToken": "valid_old_token",
            "refreshToken": "valid_old_refresh",
            "accessTokenExpires": 1000,
            "schoolId": "sch_123"
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(init_state, f)

        client = lejian.Client(dict(init_state))
        mock_body = json.dumps({
            "code": 0,
            "message": "OK",
            "data": {
                # 缺失 accessToken
                "refreshToken": "new_rf",
                "accessTokenExpires": int(time.time() * 1000) + 7200000
            }
        })

        with patch("lejian._request", return_value=(200, mock_body)), \
             patch("lejian.STATE", self.state_path):
            with self.assertRaises(RuntimeError) as ctx:
                client.refresh()
            self.assertIn("缺失 accessToken", str(ctx.exception))

        # 验证内存与磁盘均保持原样
        self.assertEqual(client.s["accessToken"], "valid_old_token")
        with open(self.state_path, "r", encoding="utf-8") as f:
            disk_state = json.load(f)
        self.assertEqual(disk_state["accessToken"], "valid_old_token")

    def test_TK02_refresh_invalid_expires_raises(self):
        """TK02: Token 刷新响应缺少有效的过期时间戳时抛出明确异常"""
        init_state = {
            "accessToken": "tok",
            "refreshToken": "ref",
            "accessTokenExpires": 1000
        }
        client = lejian.Client(dict(init_state))
        mock_body = json.dumps({
            "code": 0,
            "message": "OK",
            "data": {
                "accessToken": "new_tok",
                "refreshToken": "new_rf",
                "accessTokenExpires": 0  # 非法过期时间
            }
        })

        with patch("lejian._request", return_value=(200, mock_body)), \
             patch("lejian.STATE", self.state_path):
            with self.assertRaises(RuntimeError) as ctx:
                client.refresh()
            self.assertIn("accessTokenExpires", str(ctx.exception))

    def test_TK03_refresh_rotation_and_backup(self):
        """TK03: RefreshToken 轮换成功时正确持久化并生成 .bak 备份"""
        init_state = {
            "accessToken": "tok_old",
            "refreshToken": "ref_old",
            "accessTokenExpires": 1000
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(init_state, f)

        client = lejian.Client(dict(init_state))
        new_exp = int(time.time() * 1000) + 7200000
        mock_body = json.dumps({
            "code": 0,
            "data": {
                "accessToken": "tok_new_123",
                "refreshToken": "ref_new_rotated_456",
                "accessTokenExpires": new_exp
            }
        })

        with patch("lejian._request", return_value=(200, mock_body)), \
             patch("lejian.STATE", self.state_path):
            new_tok = client.refresh()
            self.assertEqual(new_tok, "tok_new_123")
            self.assertEqual(client.s["refreshToken"], "ref_new_rotated_456")

        # 检查新文件与备份文件
        bak_file = self.state_path + ".bak"
        self.assertTrue(os.path.exists(bak_file))
        with open(bak_file, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
        self.assertEqual(bak_data["accessToken"], "tok_old")

        with open(self.state_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertEqual(disk_data["accessToken"], "tok_new_123")
        self.assertEqual(disk_data["refreshToken"], "ref_new_rotated_456")

    def test_TK04_refresh_persistence_failure_preserves_old_state(self):
        """TK04: 新 Token 持久化写入失败时，拒绝更新内存中的 client.s"""
        init_state = {
            "accessToken": "tok_old",
            "refreshToken": "ref_old",
            "accessTokenExpires": 1000
        }
        client = lejian.Client(dict(init_state))
        mock_body = json.dumps({
            "code": 0,
            "data": {
                "accessToken": "tok_new",
                "refreshToken": "ref_new",
                "accessTokenExpires": int(time.time() * 1000) + 7200000
            }
        })

        with patch("lejian._request", return_value=(200, mock_body)), \
             patch("lejian.save_json", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                client.refresh()

        # 内存中依然是旧 Token
        self.assertEqual(client.s["accessToken"], "tok_old")

    def test_TK05_concurrent_ensure_token_refreshes_only_once(self):
        """TK05: 多个线程并发调用 ensure_token，只触发一次物理网络刷新"""
        expired_state = {
            "accessToken": "tok_old",
            "refreshToken": "ref_old",
            "accessTokenExpires": int(time.time() * 1000) - 1000  # 已过期
        }
        client = lejian.Client(dict(expired_state))

        refresh_call_count = 0
        call_lock = threading.Lock()

        def mock_request_fn(*args, **kwargs):
            nonlocal refresh_call_count
            with call_lock:
                refresh_call_count += 1
            time.sleep(0.05)  # 模拟网络延迟
            return 200, json.dumps({
                "code": 0,
                "data": {
                    "accessToken": "tok_shared_fresh",
                    "refreshToken": "ref_shared_fresh",
                    "accessTokenExpires": int(time.time() * 1000) + 7200000
                }
            })

        threads = []
        results = [None] * 5

        def worker(idx):
            with patch("lejian._request", side_effect=mock_request_fn), \
                 patch("lejian.save_json"):
                results[idx] = client.ensure_token(margin=1200)

        for i in range(5):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # 验证所有线程获得的新 Token 一致
        for res in results:
            self.assertEqual(res, "tok_shared_fresh")
        # 物理刷新只发生了一次
        self.assertEqual(refresh_call_count, 1)

    def test_TK06_refresh_html_502_error_handling(self):
        """TK06: 网关返回 502 HTML 错误页时，抛出格式明确的异常而不是未处理的崩溃"""
        init_state = {"refreshToken": "ref_old", "accessTokenExpires": 1000}
        client = lejian.Client(dict(init_state))
        html_502 = "<html><head><title>502 Bad Gateway</title></head><body>502</body></html>"

        with patch("lejian._request", return_value=(502, html_502)):
            with self.assertRaises(RuntimeError) as ctx:
                client.refresh()
            self.assertIn("502", str(ctx.exception))

    # =========================================================================
    # Phase 3: 课表冲突判断与提交前三态校验 (CF01 ~ CF05)
    # =========================================================================

    def test_CF01_activity_slot_invalid_timestamps_rejected(self):
        """CF01: activity_slot 对时间戳为 0、负数或 end <= start 的非法活动直接返回 None"""
        # 正常活动
        normal_act = {
            "startTime": 1793544000000,
            "endTime": 1793547600000
        }
        self.assertIsNotNone(lejian.activity_slot(normal_act))

        # 非法时间戳
        self.assertIsNone(lejian.activity_slot({"startTime": 0, "endTime": 1000}))
        self.assertIsNone(lejian.activity_slot({"startTime": -100, "endTime": 1000}))
        self.assertIsNone(lejian.activity_slot({"startTime": 1000, "endTime": 500}))
        self.assertIsNone(lejian.activity_slot({"startTime": "abc", "endTime": 1000}))
        self.assertIsNone(lejian.activity_slot({}))

    def test_CF02_check_conflict_three_state_handling(self):
        """CF02: 服务端 check_conflict 三态严密校验：True冲突，False无冲突，None安全阻断"""
        client = lejian.Client({})

        # 1. 明确返回冲突 (code=200, data=True)
        with patch.object(client, "call", return_value=(200, {"code": 0, "data": True})):
            self.assertIs(client.check_conflict("act_1"), True)

        # 2. 明确返回无冲突 (code=200, data=False)
        with patch.object(client, "call", return_value=(200, {"code": 0, "data": False})):
            self.assertIs(client.check_conflict("act_2"), False)

        # 3. 接口异常 / 超时 / 500 时返回 None
        with patch.object(client, "call", return_value=(500, {"_raw": "Gateway Error"})):
            self.assertIsNone(client.check_conflict("act_3"))

        with patch.object(client, "call", return_value=(-1, {"_raw": "Timeout"})):
            self.assertIsNone(client.check_conflict("act_4"))

    def test_CF03_timeslot_half_open_interval_boundary(self):
        """CF03: TimeSlot 半开区间冲突判定：连续课程不冲突，真正重叠才冲突"""
        # 课程 A: 周一 08:00 ~ 09:35 (480 ~ 575)
        slot_a = TimeSlot(weekday=1, start="08:00", end="09:35", weeks=[[1, 16]])

        # 课程 B: 周一 09:35 ~ 11:10 (575 ~ 670) 紧接上一节，分界点接触但不重叠
        self.assertFalse(slot_a.conflict_with(week_num=5, weekday=1, start_min=575, end_min=670))

        # 课程 C: 周一 09:00 ~ 10:00 (540 ~ 600) 存在交集 (540~575 重合)，冲突！
        self.assertTrue(slot_a.conflict_with(week_num=5, weekday=1, start_min=540, end_min=600))

        # 星期不同不冲突
        self.assertFalse(slot_a.conflict_with(week_num=5, weekday=2, start_min=540, end_min=600))

    def test_CF04_timeslot_discontinuous_weeks(self):
        """CF04: 不连续周次（如 1-4 周与 9-16 周，第 6 周不冲突）准确判定"""
        slot = TimeSlot(weekday=3, start="14:00", end="15:35", weeks=[[1, 4], [9, 16]])
        start_min = 14 * 60
        end_min = 15 * 60 + 35

        # 第 3 周冲突
        self.assertTrue(slot.conflict_with(week_num=3, weekday=3, start_min=start_min, end_min=end_min))
        # 第 6 周不冲突
        self.assertFalse(slot.conflict_with(week_num=6, weekday=3, start_min=start_min, end_min=end_min))
        # 第 10 周冲突
        self.assertTrue(slot.conflict_with(week_num=10, weekday=3, start_min=start_min, end_min=end_min))

    def test_CF05_timeslot_odd_even_weeks(self):
        """CF05: 单周 (1,3,5...) 单独区间设置有效性"""
        slot_single = TimeSlot(weekday=2, start="10:00", end="11:35", weeks=[[1, 1], [3, 3], [5, 5]])
        start_min = 10 * 60
        end_min = 11 * 60 + 35

        self.assertTrue(slot_single.conflict_with(week_num=1, weekday=2, start_min=start_min, end_min=end_min))
        self.assertFalse(slot_single.conflict_with(week_num=2, weekday=2, start_min=start_min, end_min=end_min))
        self.assertTrue(slot_single.conflict_with(week_num=3, weekday=2, start_min=start_min, end_min=end_min))

    # =========================================================================
    # Phase 4: 状态机异常边界与 UNKNOWN 安全屏障 (SM01 ~ SM02)
    # =========================================================================

    def test_SM01_signup_401_enters_unknown_and_verifies_without_direct_resubmit(self):
        """SM01: 提交遇到 401 时进入 UNKNOWN 并执行只读复核，绝不盲目重复发起 signUp"""
        mock_client = MagicMock()
        req_mgr = RequestManager(mock_client)
        reg_mgr = RegistrationManager(self.state_store, req_mgr, self.audit_logger)

        # 模拟 signUp 返回 401
        mock_client.call.return_value = (401, {"message": "Unauthorized"})
        # 模拟复核时拉取到完整活动列表，该活动明确未被报名 (isRegister=False)
        mock_client.activities.return_value = lejian.ActivityList(
            [{"id": "act_401_test", "name": "401测试活动", "isRegister": False}],
            complete=True, total=1
        )

        item = {"id": "act_401_test", "name": "401测试活动"}
        ok, status, reason, _ = reg_mgr.register_activity(item)

        # 不直接判失败，而是经由只读复核确认未报名后置为 RETRYABLE
        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.RETRYABLE)
        rec = self.state_store.get_record("act_401_test")
        self.assertEqual(rec["status"], RegistrationStatus.RETRYABLE)

        # 验证没有直接再次发送 signUp
        signup_calls = [c for c in mock_client.call.call_args_list if "signUp" in str(c)]
        self.assertEqual(len(signup_calls), 1)

    def test_SM02_signup_429_enters_unknown_and_verifies(self):
        """SM02: 提交遇到 429 限流时正确归类，进入安全复核流程"""
        mock_client = MagicMock()
        req_mgr = RequestManager(mock_client)
        reg_mgr = RegistrationManager(self.state_store, req_mgr, self.audit_logger)

        mock_client.call.return_value = (429, {"message": "Too Many Requests"})
        mock_client.activities.return_value = lejian.ActivityList(
            [{"id": "act_429_test", "name": "429限流测试活动", "isRegister": False}],
            complete=True, total=1
        )

        item = {"id": "act_429_test", "name": "429限流测试活动"}
        ok, status, reason, _ = reg_mgr.register_activity(item)

        self.assertFalse(ok)
        self.assertEqual(status, RegistrationStatus.RETRYABLE)
        signup_calls = [c for c in mock_client.call.call_args_list if "signUp" in str(c)]
        self.assertEqual(len(signup_calls), 1)

    # =========================================================================
    # Phase 5: 服务端逐活动对账与历史口径隔离 (RC01 ~ RC03)
    # =========================================================================

    def test_RC01_reconcile_distinguishes_current_run_and_historical(self):
        """RC01: 结果报告严格区分本轮尝试中确认成功数与历史既有已报数量"""
        reporter = ResultReporter(run_date=datetime.date(2026, 11, 1))

        # 本轮只尝试了活动 "101"
        reporter.record_attempt("101", "本轮活动 A", "SUCCESS")

        # 服务端返回了两个已报活动: 历史报名的 "999" 和本轮报名的 "101"
        reconciled_items = [
            {"id": "999", "name": "历史活动 X", "startTime": 1793544000000},
            {"id": "101", "name": "本轮活动 A", "startTime": 1793547600000},
        ]

        reporter.set_reconciliation_result(
            reconciled_items=reconciled_items,
            unresolved_count=0,
            normal_registered=2,
            max_normal=2
        )

        sm = reporter.generate_summary()
        # 本轮确认成功为 1 节（只有 101 是本轮尝试的）
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 1)
        # 既有/历史已报为 1 节
        self.assertEqual(sm["reconciliation"]["historical_count"], 1)
        # 服务端总已报为 2 节
        self.assertEqual(sm["reconciliation"]["total_server_registered"], 2)
        # 确认失败数为 0 (本轮 1 次尝试 - 1 次成功)
        self.assertEqual(sm["reconciliation"]["confirmed_failed"], 0)

        # 控制台与 Markdown 文本均包含历史说明
        console_txt = reporter.format_console_report()
        self.assertIn("既有已报活动：1 节", console_txt)
        md_txt = reporter.save_markdown_report(report_dir=self.test_dir)
        with open(md_txt, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("既有/历史已报活动", content)

    def test_RC02_reconcile_complete_empty_list_success(self):
        """RC02: 服务端成功返回合法完整的空列表 (total=0, complete=True) 时对账成功返回 0 节"""
        mock_client = MagicMock()
        # ActivityList 模拟空且完整
        act_list = lejian.ActivityList([], complete=True, total=0, fetched=0)
        mock_client.activities.return_value = act_list

        reconciler = ReconciliationManager(
            client=mock_client,
            cfg={"max_normal": 2, "max_per_day": 1},
            timetable={},
            week_calc_fn=lambda d, tt: 1
        )

        res = reconciler.reconcile()
        self.assertTrue(res["success"])
        self.assertEqual(res["total_registered"], 0)
        self.assertEqual(res["normal_registered"], 0)
        self.assertFalse(res["exceeded"])

    def test_RC03_reconcile_records_unparsed_items(self):
        """RC03: 对账遇到 startTime 非法或缺失的已报活动时，记入 unparsed_items 不静默丢弃"""
        mock_client = MagicMock()
        act_list = lejian.ActivityList([
            {"id": "act_valid", "name": "正常活动", "isRegister": True, "startTime": 1793544000000},
            {"id": "act_bad_time", "name": "异常时间活动", "isRegister": True, "startTime": "invalid_time"},
            {"id": "act_zero_time", "name": "零时间活动", "isRegister": True, "startTime": 0},
        ], complete=True, total=3, fetched=3)
        mock_client.activities.return_value = act_list

        reconciler = ReconciliationManager(
            client=mock_client,
            cfg={"max_normal": 2, "max_per_day": 1},
            timetable={},
            week_calc_fn=lambda d, tt: 1
        )

        res = reconciler.reconcile()
        self.assertTrue(res["success"])
        self.assertEqual(res["total_registered"], 3)
        self.assertEqual(res["normal_registered"], 1)
        self.assertEqual(res["unparsed_count"], 2)
        unparsed_ids = [u["id"] for u in res["unparsed_items"]]
        self.assertIn("act_bad_time", unparsed_ids)
        self.assertIn("act_zero_time", unparsed_ids)


if __name__ == "__main__":
    unittest.main()
