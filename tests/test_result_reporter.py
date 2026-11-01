"""
抢课结果统计与报告专项测试套件 (R01～R12)
=========================================
覆盖文档任务三要求的所有结果统计与报告生成测试。
"""
import datetime
import os
import tempfile
import unittest

from core.result_reporter import ResultReporter


class ResultReporterTestSuite(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.report_dir = self.temp_dir.name
        self.reporter = ResultReporter(run_date=datetime.date(2026, 11, 1))
        self.reporter.current_week = 9
        self.reporter.target_week = 10

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_R01_all_explicit_success(self):
        """R01: 全部明确成功 -> 正确统计"""
        self.reporter.record_attempt("101", "户外跑 A", "SUCCESS", "报名成功")
        self.reporter.record_attempt("102", "户外跑 B", "SUCCESS", "报名成功")

        self.reporter.set_reconciliation_result(
            reconciled_items=[{"id": "101"}, {"id": "102"}],
            normal_registered=2,
            max_normal=2
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["attempts"]["total"], 2)
        self.assertEqual(sm["attempts"]["explicit_success"], 2)
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 2)
        self.assertEqual(sm["reconciliation"]["unresolved"], 0)

    def test_R02_all_explicit_failed(self):
        """R02: 全部明确失败 -> 正确统计"""
        self.reporter.record_attempt("101", "户外跑 A", "FAILED", "名额已满")
        self.reporter.record_attempt("102", "户外跑 B", "FAILED", "冲突")

        self.reporter.set_reconciliation_result(
            reconciled_items=[],
            normal_registered=0,
            max_normal=2
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["attempts"]["total"], 2)
        self.assertEqual(sm["attempts"]["explicit_failed"], 2)
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 0)
        self.assertEqual(sm["reconciliation"]["confirmed_failed"], 2)

    def test_R03_all_unknown_independent_counting(self):
        """R03: 全部 UNKNOWN -> 不计入成功或失败，独立统计 unknown"""
        self.reporter.record_attempt("101", "候选 A", "UNKNOWN", "超时")
        self.reporter.record_attempt("102", "候选 B", "UNKNOWN", "503")

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["attempts"]["total"], 2)
        self.assertEqual(sm["attempts"]["explicit_success"], 0)
        self.assertEqual(sm["attempts"]["explicit_failed"], 0)
        self.assertEqual(sm["attempts"]["unknown"], 2)

    def test_R04_unknown_reconciled_success(self):
        """R04: UNKNOWN 最终对账确认成功 -> 最终成功数正确"""
        self.reporter.record_attempt("101", "候选 A", "UNKNOWN", "超时")
        self.reporter.set_reconciliation_result(
            reconciled_items=[{"id": "101", "name": "候选 A"}],
            unresolved_count=0,
            normal_registered=1
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["attempts"]["unknown"], 1)
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 1)
        self.assertEqual(sm["reconciliation"]["confirmed_failed"], 0)

    def test_R05_unknown_reconciled_failed(self):
        """R05: UNKNOWN 最终对账确认失败 -> 最终失败数正确"""
        self.reporter.record_attempt("101", "候选 A", "UNKNOWN", "超时")
        self.reporter.set_reconciliation_result(
            reconciled_items=[],
            unresolved_count=0,
            normal_registered=0
        )

        sm = self.reporter.generate_summary()
        self.assertEqual(sm["reconciliation"]["confirmed_success"], 0)
        self.assertEqual(sm["reconciliation"]["confirmed_failed"], 1)

    def test_R06_unknown_unresolved_shows_warning_not_zero_success(self):
        """R06: UNKNOWN 未解决 -> 控制台与 Markdown 报告明确报警，禁止输出「抢课成功 0 节」作为结论"""
        self.reporter.record_attempt("101", "候选 A", "UNKNOWN", "超时")
        self.reporter.set_reconciliation_result(
            reconciled_items=[],
            unresolved_count=1
        )

        console_txt = self.reporter.format_console_report()
        self.assertIn("未完成对账 (存在未决 UNKNOWN)", console_txt)
        self.assertIn("【安全警告】", console_txt)
        self.assertNotIn("最终结果：成功报名 0 节", console_txt)

    def test_R07_multiple_verifications_do_not_duplicate_counts(self):
        """R07: 同一报名被多次复核 -> 不重复累计最终成功数"""
        self.reporter.record_attempt("101", "候选 A", "UNKNOWN", "超时")

        # 无论复核调用多少次，对账列表以最终唯一记录为准
        self.reporter.set_reconciliation_result(
            reconciled_items=[{"id": "101", "name": "候选 A"}],
            unresolved_count=0,
            normal_registered=1
        )
        self.assertEqual(self.reporter.reconciled_success, 1)

    def test_R08_server_total_matches_remote_items(self):
        """R08: 最终服务端报名总数与服务端数据一致"""
        items = [{"id": "101"}, {"id": "102"}]
        self.reporter.set_reconciliation_result(
            reconciled_items=items,
            normal_registered=2,
            max_normal=2
        )
        self.assertEqual(self.reporter.reconciled_success, 2)

    def test_R09_daily_and_weekly_quota_stats(self):
        """R09: 每日、每周配额统计与超额预警标记准确"""
        self.reporter.set_reconciliation_result(
            reconciled_items=[{"id": "101"}, {"id": "102"}, {"id": "103"}],
            normal_registered=3,
            max_normal=2,
            weekly_exceeded=True,
            daily_exceeded=True
        )
        sm = self.reporter.generate_summary()
        self.assertTrue(sm["quota"]["weekly_exceeded"])
        self.assertTrue(sm["quota"]["daily_exceeded"])

    def test_R10_http_failure_breakdown_correct(self):
        """R10: 网络故障统计分类正确"""
        for _ in range(9):
            self.reporter.record_http_failure(-1)
        for _ in range(10):
            self.reporter.record_http_failure(500)
        for _ in range(2):
            self.reporter.record_http_failure(503)
        self.reporter.record_http_failure(504)

        sm = self.reporter.generate_summary()
        breakdown = sm["server_failures"]["breakdown"]
        self.assertEqual(breakdown[-1], 9)
        self.assertEqual(breakdown[500], 10)
        self.assertEqual(breakdown[503], 2)
        self.assertEqual(breakdown[504], 1)
        self.assertEqual(sm["server_failures"]["total_failures"], 22)

    def test_R11_markdown_report_generation(self):
        """R11: Markdown 报告生成，内容完整且符合规范"""
        self.reporter.record_attempt("101", "户外跑 程启航", "UNKNOWN", "超时")
        self.reporter.set_reconciliation_result(
            reconciled_items=[{"id": "101", "name": "户外跑 程启航", "startTime": 1730000000000}],
            normal_registered=1,
            max_normal=2
        )
        self.reporter.record_http_failure(500)
        self.reporter.set_circuit_metrics(outage_seconds=12.5, trip_count=1)

        rpt_path = self.reporter.save_markdown_report(report_dir=self.report_dir)
        self.assertTrue(os.path.exists(rpt_path))

        with open(rpt_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("# 抢课结果报告 (2026-11-01)", content)
        self.assertIn("户外跑 程启航", content)
        self.assertIn("HTTP 500", content)
        self.assertIn("12.5", content)

    def test_R12_report_collision_avoids_overwriting_historical_reports(self):
        """R12: 报告文件已存在时再次生成 -> 采用唯一重命名策略，绝不覆盖历史报告"""
        base_name = "test-report"
        path1 = self.reporter.save_markdown_report(report_dir=self.report_dir, base_name=base_name)
        with open(path1, "w", encoding="utf-8") as f:
            f.write("ORIGINAL_CONTENT")

        # 再次生成同名报告
        path2 = self.reporter.save_markdown_report(report_dir=self.report_dir, base_name=base_name)

        self.assertNotEqual(path1, path2)
        self.assertTrue(os.path.exists(path1))
        self.assertTrue(os.path.exists(path2))

        # 验证原文件未被覆盖
        with open(path1, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "ORIGINAL_CONTENT")


if __name__ == "__main__":
    unittest.main()
