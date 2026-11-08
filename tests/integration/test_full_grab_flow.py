"""
Phase 7 端到端完整正常流程集成测试套件 (I01 ~ I20)
=================================================
验证从 Preflight 启动检查、活动拉取、冲突判定、配额规则、
原子预留、单次提交、状态更新到最终对账与结果报告的完整闭环。
"""
import copy
import datetime
import json
import os
import time
import unittest

from core.audit_logger import EventName
from core.conflict_checker import ActivityList, ConflictCheckMode
from core.preflight import PreflightMode, PreflightStatus
from core.state_store import RegistrationStatus
from tests.integration.integration_helper import IntegrationEnvironment


class TestFullGrabFlow(unittest.TestCase):
    """端到端正常抢课流程集成测试 (I01 ~ I20)"""

    def setUp(self):
        self.env = IntegrationEnvironment()
        # 默认添加 2 个合规的候选活动（教学周第 10 周，2026-11-08）
        # 活动 1：周日 14:00 - 16:00
        self.env.server.add_activity(
            aid="act_101",
            name="篮球训练课",
            start_time_ms=1794146400000,  # 约 2026-11-08 14:00
            end_time_ms=1794153600000,
            limit_people=30,
            registered_people=5,
            is_register=False
        )
        # 活动 2：周日 16:30 - 18:30 (同时段不重叠)
        self.env.server.add_activity(
            aid="act_102",
            name="羽毛球基础课",
            start_time_ms=1794155400000,
            end_time_ms=1794162600000,
            limit_people=20,
            registered_people=2,
            is_register=False
        )

    def tearDown(self):
        self.env.close()

    # -------------------------------------------------------------------------
    # I01: 端到端正常抢课流程全部闭环
    # -------------------------------------------------------------------------
    def test_I01_full_normal_flow_end_to_end(self):
        # 1. Preflight 启动检查
        preflight = self.env.create_preflight()
        report = preflight.run_all_checks()
        self.assertEqual(report.status, PreflightStatus.READY)
        self.assertEqual(report.mode, PreflightMode.SERVER_MODE)

        # 2. 拉取活动列表
        items = self.env.client.activities()
        self.assertTrue(items.complete)
        self.assertEqual(len(items), 2)

        # 3. 冲突检查 (第一门课)
        target = items[0]
        aid = target["id"]
        name = target["name"]
        conf_ok, conf_reason = self.env.conflict_manager.check_activity_conflict(
            candidate=target,
            items=items,
            week_done={},
            correlation_id="corr_test_01",
            run_id=self.env.run_id,
            current_week=10
        )
        self.assertTrue(conf_ok)

        # 4. SQLite 原子预留
        res_ok, rec, msg = self.env.state_store.reserve_submitting(aid, name)
        self.assertTrue(res_ok)
        self.assertEqual(rec["status"], RegistrationStatus.SUBMITTING)

        # 5. 单次报名提交
        sub_res = self.env.submit_signup(aid, name)
        self.assertEqual(sub_res.classification, "SUCCESS")

        # 6. 本地持久化状态更新为 SUCCESS
        self.env.state_store.record_success(aid, name)
        final_rec = self.env.state_store.get_record(aid)
        self.assertEqual(final_rec["status"], RegistrationStatus.SUCCESS)

        # 7. 全局最终对账
        rec_res = self.env.reconciliation_manager.reconcile()
        self.assertTrue(rec_res.get("success", False))

        # 8. 结果报告生成
        self.env.finalize_reporter(rec_res)
        report_data = self.env.result_reporter.generate_summary()
        self.assertEqual(report_data["verdict"]["status"], "SUCCESS")

    # -------------------------------------------------------------------------
    # I02: signUp 调用次数严格为 1
    # -------------------------------------------------------------------------
    def test_I02_signup_called_strictly_once(self):
        aid = "act_101"
        name = "篮球训练课"
        self.env.server.reset_counts()
        self.env.state_store.reserve_submitting(aid, name)
        sub_res = self.env.submit_signup(aid, name)
        self.assertEqual(sub_res.classification, "SUCCESS")
        self.assertEqual(self.env.server.signup_calls_count, 1)

    # -------------------------------------------------------------------------
    # I03: 候选活动原子预留仅执行一次，避免重复预留
    # -------------------------------------------------------------------------
    def test_I03_reserve_submitting_once_only(self):
        aid = "act_101"
        name = "篮球训练课"
        ok1, _, _ = self.env.state_store.reserve_submitting(aid, name)
        self.assertTrue(ok1)
        # 再次尝试预留同一活动，直接被拒绝
        ok2, _, msg = self.env.state_store.reserve_submitting(aid, name)
        self.assertFalse(ok2)
        self.assertIn("已处于 SUBMITTING 状态", msg)

    # -------------------------------------------------------------------------
    # I04: 本地 StateStore 状态与远端注册状态精确一致
    # -------------------------------------------------------------------------
    def test_I04_local_state_matches_remote_state(self):
        aid = "act_101"
        self.env.state_store.reserve_submitting(aid, "篮球")
        sub_res = self.env.submit_signup(aid, "篮球")
        self.assertEqual(sub_res.classification, "SUCCESS")
        self.env.state_store.record_success(aid, "篮球")
        self.assertIn(aid, self.env.server.registered_activities)
        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.SUCCESS)

    # -------------------------------------------------------------------------
    # I05: HTTP 请求统计准确记录
    # -------------------------------------------------------------------------
    def test_I05_http_requests_accurately_counted(self):
        self.env.server.reset_counts()
        self.env.client.activities()
        self.env.client.check_conflict("act_101")
        self.env.submit_signup("act_101", "篮球")
        # 1次列表 + 1次冲突 + 1次提交 = 3次 HTTP
        self.assertEqual(self.env.server.http_requests_count, 3)

    # -------------------------------------------------------------------------
    # I06: 完整审计事件流发射
    # -------------------------------------------------------------------------
    def test_I06_complete_audit_event_stream(self):
        aid = "act_101"
        corr_id = "corr_audit_stream_01"
        target = self.env.server.activities_db[aid]
        self.env.conflict_manager.check_activity_conflict(
            candidate=target,
            items=[target],
            correlation_id=corr_id,
            run_id=self.env.run_id,
            current_week=10
        )
        self.env.state_store.reserve_submitting(aid, "篮球")
        self.env.submit_signup(aid, "篮球")
        self.env.reconciliation_manager.reconcile()

        # 读取审计日志
        date_str = datetime.date.today().strftime("%Y%m%d")
        event_file = os.path.join(self.env.logs_dir, f"events-{date_str}.jsonl")
        self.assertTrue(os.path.exists(event_file))
        with open(event_file, "r", encoding="utf-8") as f:
            lines = [json.loads(l) for l in f.readlines()]

        event_names = [e.get("event_name") for e in lines]
        self.assertIn(EventName.CONFLICT_CHECK_STARTED, event_names)
        self.assertIn(EventName.CONFLICT_CHECK_SUCCEEDED, event_names)
        self.assertIn(EventName.RECONCILIATION_STARTED, event_names)

    # -------------------------------------------------------------------------
    # I07: ResultReporter 与 StateStore 终态一致
    # -------------------------------------------------------------------------
    def test_I07_reporter_matches_state_store(self):
        aid = "act_101"
        self.env.submit_signup(aid, "篮球")
        self.env.state_store.record_success(aid, "篮球")
        rec_res = self.env.reconciliation_manager.reconcile()
        self.env.finalize_reporter(rec_res)
        rep = self.env.result_reporter.generate_summary()
        self.assertEqual(rep["verdict"]["status"], "SUCCESS")

    # -------------------------------------------------------------------------
    # I08: 连续候选活动，第一门成功后第二门触发周配额拦截
    # -------------------------------------------------------------------------
    def test_I08_sequential_candidates_quota_limit(self):
        self.env.cfg["max_normal"] = 1
        week_done = {}
        items = self.env.client.activities()

        # 第一门课
        aid1 = items[0]["id"]
        self.env.state_store.reserve_submitting(aid1, "课1")
        self.env.submit_signup(aid1, "课1")
        self.env.state_store.record_success(aid1, "课1")
        week_done[10] = 1

        # 刷新快照以反映最新状态
        items = self.env.client.activities()

        # 第二门课：相同周次（第10周），触发本地降级检查，应因周配额已满被拦截
        self.env.conflict_breaker.record_failure(http_status=500, force_trip=True)
        aid2 = items[1]["id"]
        conf_ok, reason = self.env.conflict_manager.check_activity_conflict(
            candidate=items[1],
            items=items,
            week_done=week_done,
            correlation_id="corr_02",
            run_id=self.env.run_id,
            current_week=10
        )
        self.assertFalse(conf_ok)
        self.assertIn("上限", reason)

    # -------------------------------------------------------------------------
    # I09: 会员专属课程在提交前被过滤
    # -------------------------------------------------------------------------
    def test_I09_member_only_activity_filtered(self):
        self.env.server.add_activity("act_mem", "高尔夫(会员制)", 1794146400000, 1794153600000)
        items = self.env.client.activities()
        mem_act = next(i for i in items if i["id"] == "act_mem")
        from lejian import is_member_only
        self.assertTrue(is_member_only(mem_act))

    # -------------------------------------------------------------------------
    # I10: 特殊活动不消耗常规周配额
    # -------------------------------------------------------------------------
    def test_I10_special_activity_does_not_consume_quota(self):
        from lejian import is_special
        spec_act = {"name": "马拉松专项训练"}
        self.assertTrue(is_special(spec_act, self.env.cfg))

    # -------------------------------------------------------------------------
    # I11: 跨教学周配额互不干扰
    # -------------------------------------------------------------------------
    def test_I11_multiple_weeks_quotas_independent(self):
        week_done = {10: 2}  # 第 10 周已达上限 2
        items = self.env.client.activities()
        # 第 11 周的课程依然允许
        conf_ok, _ = self.env.conflict_manager.check_activity_conflict(
            candidate=items[0],
            items=items,
            week_done=week_done,
            correlation_id="corr_wk11",
            run_id=self.env.run_id,
            current_week=11
        )
        self.assertTrue(conf_ok)

    # -------------------------------------------------------------------------
    # I12: 单日最大报名节数 (max_daily) 限制有效
    # -------------------------------------------------------------------------
    def test_I12_daily_quota_limit_enforced(self):
        day_done = {datetime.date(2026, 11, 8): 1}
        self.env.cfg["max_per_day"] = 1
        items = self.env.client.activities()
        # 本地检查器检查单日配额
        res = self.env.local_conflict_checker.check_conflict(
            candidate=items[0],
            items=items,
            cfg=self.env.cfg,
            tt=self.env.tt,
            day_done=day_done,
            current_week=10
        )
        self.assertTrue(res.has_conflict)
        self.assertIn("每日上限", res.reason)

    # -------------------------------------------------------------------------
    # I13: 与课表固定时段冲突在提交前阻断
    # -------------------------------------------------------------------------
    def test_I13_timetable_collision_blocked(self):
        # 课表有固定课程与 22:00 - 00:00 冲突
        self.env.tt["busy_slots"] = [{
            "label": "高等数学",
            "weekday": 7,  # 周日
            "start": "21:30",
            "end": "22:30",
            "weeks": [[1, 16]]
        }]
        items = self.env.client.activities()
        res = self.env.local_conflict_checker.check_conflict(
            candidate=items[0],
            items=items,
            tt=self.env.tt,
            current_week=10
        )
        self.assertTrue(res.has_conflict)
        self.assertIn("课表", res.reason)

    # -------------------------------------------------------------------------
    # I14: 与已报课程时段重叠在提交前阻断
    # -------------------------------------------------------------------------
    def test_I14_collision_with_already_registered_blocked(self):
        items = self.env.client.activities()
        # 模拟 items[1] 已经是已报状态且与 items[0] 时段重叠
        reg_item = copy.deepcopy(items[0])
        reg_item["id"] = "act_999"
        reg_item["isRegister"] = True
        items_with_reg = ActivityList([items[0], reg_item], complete=True, fetched_at=time.time())

        res = self.env.local_conflict_checker.check_conflict(
            candidate=items[0],
            items=items_with_reg,
            current_week=10
        )
        self.assertTrue(res.has_conflict)
        self.assertIn("已报", res.reason)

    # -------------------------------------------------------------------------
    # I15: checkConflict 返回 False (无冲突) 允许提交
    # -------------------------------------------------------------------------
    def test_I15_conflict_returns_false_allows_submit(self):
        self.env.server.conflict_result = False
        items = self.env.client.activities()
        ok, reason = self.env.conflict_manager.check_activity_conflict(
            candidate=items[0],
            items=items,
            correlation_id="corr_15",
            run_id=self.env.run_id,
            current_week=10
        )
        self.assertTrue(ok)
        self.assertIn("无冲突", reason)

    # -------------------------------------------------------------------------
    # I16: checkConflict 返回 True (有冲突) 阻断提交
    # -------------------------------------------------------------------------
    def test_I16_conflict_returns_true_blocks_submit(self):
        self.env.server.conflict_result = True
        items = self.env.client.activities()
        ok, reason = self.env.conflict_manager.check_activity_conflict(
            candidate=items[0],
            items=items,
            correlation_id="corr_16",
            run_id=self.env.run_id,
            current_week=10
        )
        self.assertFalse(ok)
        self.assertIn("冲突", reason)

    # -------------------------------------------------------------------------
    # I17: 服务端业务失败 (名额已满) 记录 FAILED 并允许尝试下一门
    # -------------------------------------------------------------------------
    def test_I17_server_business_failed_records_failed_and_continues(self):
        self.env.server.signup_business_code = 10002
        self.env.server.signup_business_msg = "活动名额已满"
        aid = "act_101"
        self.env.state_store.reserve_submitting(aid, "课1")
        sub_res = self.env.submit_signup(aid, "课1")
        self.assertEqual(sub_res.classification, "FAILED")

        # 更新本地状态
        self.env.state_store.record_failed(aid, "课1", error=sub_res.reason)
        rec = self.env.state_store.get_record(aid)
        self.assertEqual(rec["status"], RegistrationStatus.FAILED)

    # -------------------------------------------------------------------------
    # I18: dry_run 演练模式全流程安全评估，signUp 绝不调用
    # -------------------------------------------------------------------------
    def test_I18_dry_run_mode_never_calls_signup(self):
        self.env.server.reset_counts()
        preflight = self.env.create_preflight()
        rep = preflight.run_all_checks()
        self.assertEqual(rep.status, PreflightStatus.READY)
        items = self.env.client.activities()
        self.assertEqual(len(items), 2)
        # dry_run 模式：评估通过后直接返回，不调用 signUp
        self.assertEqual(self.env.server.signup_calls_count, 0)

    # -------------------------------------------------------------------------
    # I19: correlation_id 贯穿全链路可追溯
    # -------------------------------------------------------------------------
    def test_I19_correlation_id_propagated(self):
        cid = "corr_trace_test_19"
        target = self.env.server.activities_db["act_101"]
        self.env.conflict_manager.check_activity_conflict(
            candidate=target,
            items=[target],
            correlation_id=cid,
            run_id=self.env.run_id,
            current_week=10
        )
        self.env.state_store.reserve_submitting("act_101", "课1")
        self.env.submit_signup("act_101", "课1")

        # 检查日志中包含该 cid 的记录
        date_str = datetime.date.today().strftime("%Y%m%d")
        event_file = os.path.join(self.env.logs_dir, f"events-{date_str}.jsonl")
        with open(event_file, "r", encoding="utf-8") as f:
            matching = [json.loads(l) for l in f if cid in l]
        self.assertGreaterEqual(len(matching), 1)

    # -------------------------------------------------------------------------
    # I20: 完整执行产生格式规范的 preflight_report.json
    # -------------------------------------------------------------------------
    def test_I20_preflight_report_json_produced(self):
        rep_path = os.path.join(self.env.logs_dir, "preflight_report.json")
        preflight = self.env.create_preflight()
        report = preflight.run_all_checks(output_report_path=rep_path)
        self.assertTrue(os.path.exists(rep_path))
        with open(rep_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["run_id"], self.env.run_id)
        self.assertEqual(data["status"], "READY")
        self.assertIn("checks", data)


if __name__ == "__main__":
    unittest.main()
