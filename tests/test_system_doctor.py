"""
乐健体育 · 独立系统体检与只读健康诊断自动化测试套件 (D01 ~ D19).
==============================================================
覆盖执行计划 M4 (第四阶段) 全部验收指标：
1. 运行环境与依赖检查 (D01, D11)
2. 本地存储与配置检查 (D08, D09, D10)
3. 凭据合法性与 DPAPI 解密 (D02, D03, D04)
4. 会话状态与 Token 剩余时效 (D05, D06, D07)
5. 事务安全性与未决日志 (D12)
6. 在线服务与只读 Token 验真 (D13, D14)
7. 脱敏安全与只读保证 (D15, D17)
8. 机器可读字典与 CLI 路由 (D16, D18, D19)
"""
import base64
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.system_doctor import CheckItem, DoctorReport, SystemDoctor


class SystemDoctorTestSuite(unittest.TestCase):
    """M4 系统体检独立测试套件"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_doctor_")
        self.data_dir = os.path.join(self.test_dir, "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "registration.db")
        self.cred_path = os.path.join(self.test_dir, "credential.json")
        self.state_path = os.path.join(self.test_dir, "state.json")
        self.config_path = os.path.join(self.test_dir, "config.json")
        self.timetable_path = os.path.join(self.test_dir, "timetable.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _setup_healthy_environment(self):
        """构造完全健康的测试环境"""
        # 1. credential.json (支持 Windows DPAPI 密文或安全明文)
        raw_sign = "sig_mock_healthy_sign_abcdef1234567890abcdef1234567890abcdef12"
        from core.credential_storage import is_windows
        if is_windows():
            try:
                from core.credential_storage import dpapi_protect
                enc_b64 = base64.b64encode(dpapi_protect(raw_sign.encode("utf-8"))).decode("utf-8")
                cred_data = {
                    "account": "18612347060",
                    "signDigitalEncrypted": enc_b64,
                    "entrance": "1",
                    "timestamp": "2026-11-01 20:00:00",
                }
            except Exception:
                cred_data = {
                    "account": "18612347060",
                    "signDigital": raw_sign,
                    "entrance": "1",
                    "timestamp": "2026-11-01 20:00:00",
                }
        else:
            cred_data = {
                "account": "18612347060",
                "signDigital": raw_sign,
                "entrance": "1",
                "timestamp": "2026-11-01 20:00:00",
            }
        with open(self.cred_path, "w", encoding="utf-8") as f:
            json.dump(cred_data, f)

        # 2. state.json (未过期)
        exp_future = int((time.time() + 7200) * 1000)
        state_data = {
            "accessToken": "tok_mock_healthy_access_token_abcdef1234567890",
            "refreshToken": "ref_mock_healthy_refresh_token_abcdef1234567890",
            "schoolId": "sch_bj_pe_univ",
            "semesterId": "sem_2026_autumn",
            "accessTokenExpires": exp_future,
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f)

        # 3. config.json
        cfg_data = {
            "targets": ["户外跑", "跳绳", "排球"],
            "poll_interval_ms": 500,
            "max_normal": 2,
            "max_per_day": 1,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(cfg_data, f)

        # 4. timetable.json
        tt_data = {
            "week1_monday": "2026-09-07",
            "total_weeks": 16,
            "courses": [],
        }
        with open(self.timetable_path, "w", encoding="utf-8") as f:
            json.dump(tt_data, f)

        # 5. SQLite registration.db
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("""
                CREATE TABLE registration_records (
                    activity_id TEXT PRIMARY KEY,
                    activity_name TEXT,
                    status TEXT NOT NULL
                );
            """)
            conn.execute("INSERT INTO registration_records VALUES ('act_1', '户外跑', 'SUCCESS');")
        conn.close()

    # -------------------------------------------------------------------------
    # D01: 完整健康环境体检
    # -------------------------------------------------------------------------
    def test_D01_healthy_environment_overall_healthy(self):
        """D01: 所有本地与在线指标正常时，总体健康评估应为 HEALTHY 且各项带 [√]"""
        self._setup_healthy_environment()

        class MockResp:
            def __init__(self, data_dict, status=200):
                self.status = status
                self._data = json.dumps(data_dict).encode("utf-8")

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_urlopen_fn(req, *args, **kwargs):
            url = getattr(req, "full_url", str(req))
            if "education/semester/getCurrent" in url:
                return MockResp({"code": 0, "data": {"yearRange": "2026年秋季学期", "id": "sem_123"}})
            return MockResp({"status": "ok"})

        doctor = SystemDoctor(root_dir=self.test_dir)
        with patch("urllib.request.urlopen", side_effect=mock_urlopen_fn):
            report = doctor.run_diagnostics(online_probe=True)

        self.assertEqual(report.overall_status, "HEALTHY")
        text = report.format_report()
        self.assertIn("总体健康评估: 正常 (HEALTHY)", text)
        self.assertIn("186****7060", text)
        self.assertIn("2026年秋季学期", text)
        self.assertIn("包含 1 条抢课记录", text)

    # -------------------------------------------------------------------------
    # D02: 缺失 credential.json
    # -------------------------------------------------------------------------
    def test_D02_missing_credential_prompts_setup(self):
        """D02: 缺失 credential.json 时给出 WARN 并建议运行 setup 首次向导"""
        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        cred_item = next((c for c in report.checks if c.name == "凭据文件"), None)
        self.assertIsNotNone(cred_item)
        self.assertEqual(cred_item.status, "WARN")
        self.assertIn("setup", cred_item.suggestion)

    # -------------------------------------------------------------------------
    # D03: credential.json 损坏或非法 JSON
    # -------------------------------------------------------------------------
    def test_D03_corrupted_credential_reports_error(self):
        """D03: credential.json 损坏时给出 ERROR 并建议 reinit 重新抓取"""
        with open(self.cred_path, "w", encoding="utf-8") as f:
            f.write("{not a valid json")

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        self.assertEqual(report.overall_status, "ERROR")
        cred_item = next((c for c in report.checks if c.name == "凭据文件"), None)
        self.assertIsNotNone(cred_item)
        self.assertEqual(cred_item.status, "ERROR")
        self.assertIn("reinit", cred_item.suggestion)

    # -------------------------------------------------------------------------
    # D04: DPAPI 密文解密失败
    # -------------------------------------------------------------------------
    def test_D04_dpapi_decrypt_failure_reports_error(self):
        """D04: DPAPI 密文无法解密时标红 ERROR 并提示跨用户或重新初始化"""
        cred_data = {
            "account": "18612347060",
            "signDigitalEncrypted": base64.b64encode(b"invalid_ciphertext").decode(),
        }
        with open(self.cred_path, "w", encoding="utf-8") as f:
            json.dump(cred_data, f)

        doctor = SystemDoctor(root_dir=self.test_dir)
        with patch("core.system_doctor.dpapi_unprotect", side_effect=OSError("CryptUnprotectData failed")):
            report = doctor.run_diagnostics(online_probe=False)

        self.assertEqual(report.overall_status, "ERROR")
        item = next((c for c in report.checks if c.name == "凭据保护模式"), None)
        self.assertIsNotNone(item)
        self.assertEqual(item.status, "ERROR")
        self.assertIn("reinit", item.suggestion)

    # -------------------------------------------------------------------------
    # D05: 缺失 state.json
    # -------------------------------------------------------------------------
    def test_D05_missing_state_prompts_quick_login(self):
        """D05: 缺失 state.json 时提示未生成会话，建议免抓包快速登录"""
        cred_data = {
            "account": "18612347060",
            "signDigital": "sig_mock",
        }
        with open(self.cred_path, "w", encoding="utf-8") as f:
            json.dump(cred_data, f)

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        st_item = next((c for c in report.checks if c.name == "会话文件"), None)
        self.assertIsNotNone(st_item)
        self.assertEqual(st_item.status, "WARN")
        self.assertIn("快速登录", st_item.suggestion)

    # -------------------------------------------------------------------------
    # D06: Token 已过期但 RefreshToken 存在
    # -------------------------------------------------------------------------
    def test_D06_token_expired_with_refresh_token(self):
        """D06: Token 过期但 RT 存在时标记 WARN 并建议 refresh 静默刷新"""
        state_data = {
            "accessToken": "tok_old",
            "refreshToken": "ref_valid",
            "schoolId": "sch_123",
            "semesterId": "sem_123",
            "accessTokenExpires": int((time.time() - 3600) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f)

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        tok_item = next((c for c in report.checks if c.name == "访问令牌 (Token)"), None)
        self.assertIsNotNone(tok_item)
        self.assertEqual(tok_item.status, "WARN")
        self.assertIn("已过期", tok_item.message)
        self.assertIn("refresh", tok_item.suggestion)

    # -------------------------------------------------------------------------
    # D07: Token 已过期且缺失 RefreshToken
    # -------------------------------------------------------------------------
    def test_D07_token_expired_without_refresh_token(self):
        """D07: Token 过期且缺失 RT 时，RT 项标记 WARN 并建议重新登录"""
        state_data = {
            "accessToken": "tok_old",
            "schoolId": "sch_123",
            "semesterId": "sem_123",
            "accessTokenExpires": int((time.time() - 3600) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f)

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        rt_item = next((c for c in report.checks if c.name == "刷新令牌 (RT)"), None)
        self.assertIsNotNone(rt_item)
        self.assertEqual(rt_item.status, "WARN")
        self.assertIn("缺少 refreshToken", rt_item.message)

    # -------------------------------------------------------------------------
    # D08: config.json 缺失或配置项非法
    # -------------------------------------------------------------------------
    def test_D08_missing_config_json_warns_default(self):
        """D08: config.json 缺失时提示使用系统默认值，给出温和 WARN"""
        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        cfg_item = next((c for c in report.checks if c.name == "任务配置文件"), None)
        self.assertIsNotNone(cfg_item)
        self.assertEqual(cfg_item.status, "WARN")
        self.assertIn("默认抢课配置", cfg_item.message)

    # -------------------------------------------------------------------------
    # D09: timetable.json 缺失
    # -------------------------------------------------------------------------
    def test_D09_missing_timetable_json_warns(self):
        """D09: timetable.json 缺失时提示未配置排课过滤"""
        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        tt_item = next((c for c in report.checks if c.name == "课表配置文件"), None)
        self.assertIsNotNone(tt_item)
        self.assertEqual(tt_item.status, "WARN")
        self.assertIn("未配置个人排课冲突过滤", tt_item.message)

    # -------------------------------------------------------------------------
    # D10: SQLite registration.db 损坏
    # -------------------------------------------------------------------------
    def test_D10_corrupted_sqlite_db_reports_error(self):
        """D10: 数据库文件损坏导致 integrity_check 失败时标红 ERROR"""
        with open(self.db_path, "wb") as f:
            f.write(b"NOT A SQLITE FILE HEADER CRASH")

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        db_item = next((c for c in report.checks if c.name == "SQLite 状态库"), None)
        self.assertIsNotNone(db_item)
        self.assertEqual(db_item.status, "ERROR")
        self.assertEqual(report.overall_status, "ERROR")
        self.assertIn("corrupt.bak", db_item.suggestion)
        self.assertIn("复核已选活动", db_item.suggestion)

    # -------------------------------------------------------------------------
    # D11: 工作目录无写权限
    # -------------------------------------------------------------------------
    def test_D11_directory_unwritable_reports_error(self):
        """D11: 模拟关键目录不可写时标红 ERROR 并给出提权修复建议"""
        doctor = SystemDoctor(root_dir=self.test_dir)
        with patch("builtins.open", side_effect=PermissionError("Access denied (mock)")):
            report = doctor.run_diagnostics(online_probe=False)

        dir_item = next((c for c in report.checks if c.name == "工作目录权限"), None)
        self.assertIsNotNone(dir_item)
        self.assertEqual(dir_item.status, "ERROR")
        self.assertIn("读写权限", dir_item.suggestion)

    # -------------------------------------------------------------------------
    # D12: 存在未决事务残留文件
    # -------------------------------------------------------------------------
    def test_D12_pending_transaction_residuals_warns(self):
        """D12: 检测到未完成事务日志或备份时给出 WARN 并建议 setup 自愈"""
        tx_log = os.path.join(self.test_dir, "credential.json.tx.json")
        with open(tx_log, "w", encoding="utf-8") as f:
            f.write('{"status": "IN_PROGRESS"}')

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        tx_item = next((c for c in report.checks if c.name == "事务状态检查"), None)
        self.assertIsNotNone(tx_item)
        self.assertEqual(tx_item.status, "WARN")
        self.assertIn("credential.json.tx.json", tx_item.message)
        self.assertIn("setup", tx_item.suggestion)

    # -------------------------------------------------------------------------
    # D13: 在线服务连通性受阻（离线）
    # -------------------------------------------------------------------------
    def test_D13_online_probe_network_offline_warns_non_blocking(self):
        """D13: 离线或弱网时给出 WARN，不阻断系统整体体检评估"""
        self._setup_healthy_environment()

        doctor = SystemDoctor(root_dir=self.test_dir)
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Network unreachable")):
            report = doctor.run_diagnostics(online_probe=True)

        net_item = next((c for c in report.checks if c.name == "乐健服务连通"), None)
        self.assertIsNotNone(net_item)
        self.assertEqual(net_item.status, "WARN")
        self.assertIn("离线", net_item.message)

    # -------------------------------------------------------------------------
    # D14: Token 在线只读验真细分区分 HTTP 401
    # -------------------------------------------------------------------------
    def test_D14_online_token_validation_server_401_nuanced_handling(self):
        """D14: 细分处理 401：无明确原因时报 WARN 提示暂时无法确定，有明确撤销时标红 ERROR"""
        self._setup_healthy_environment()

        # 1. 模拟未携带结构化原因的普通 401：避免武断断言失效，报告 WARN 与未知原因
        class MockGeneric401Error(urllib.error.HTTPError):
            def __init__(self, url):
                super().__init__(url, 401, "Unauthorized", {}, None)
            def read(self):
                return b'{"message": "Unauthorized"}'

        def mock_generic_401(req, *args, **kwargs):
            url = getattr(req, "full_url", str(req))
            if "education/semester/getCurrent" in url:
                raise MockGeneric401Error(url)
            class MockResp:
                def __init__(self): self.status = 200
                def read(self): return b"ok"
                def __enter__(self): return self
                def __exit__(self, *args): pass
            return MockResp()

        doctor = SystemDoctor(root_dir=self.test_dir)
        with patch("urllib.request.urlopen", side_effect=mock_generic_401):
            report = doctor.run_diagnostics(online_probe=True)

        token_online_item = next((c for c in report.checks if c.name == "Token 在线验真"), None)
        self.assertIsNotNone(token_online_item)
        self.assertEqual(token_online_item.status, "WARN")
        self.assertIn("未授权响应", token_online_item.message)
        self.assertIn("暂时无法确定", token_online_item.message)

        # 2. 模拟明确返回已撤销原因的 401：标红 ERROR 并确认已撤销
        class MockRevoked401Error(urllib.error.HTTPError):
            def __init__(self, url):
                super().__init__(url, 401, "Unauthorized", {}, None)
            def read(self):
                return json.dumps({"code": 40101, "message": "Token已被撤销"}).encode("utf-8")

        def mock_revoked_401(req, *args, **kwargs):
            url = getattr(req, "full_url", str(req))
            if "education/semester/getCurrent" in url:
                raise MockRevoked401Error(url)
            class MockResp:
                def __init__(self): self.status = 200
                def read(self): return b"ok"
                def __enter__(self): return self
                def __exit__(self, *args): pass
            return MockResp()

        with patch("urllib.request.urlopen", side_effect=mock_revoked_401):
            report_revoked = doctor.run_diagnostics(online_probe=True)

        token_revoked_item = next((c for c in report_revoked.checks if c.name == "Token 在线验真"), None)
        self.assertIsNotNone(token_revoked_item)
        self.assertEqual(token_revoked_item.status, "ERROR")
        self.assertIn("已被撤销", token_revoked_item.message)

    # -------------------------------------------------------------------------
    # D15: 全程脱敏安全校验
    # -------------------------------------------------------------------------
    def test_D15_security_masking_comprehensive(self):
        """D15: 诊断报告绝不输出明文账号、完整 signDigital 或完整 AccessToken"""
        self._setup_healthy_environment()

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)
        report_text = report.format_report(verbose=True)

        # 敏感信息绝不存在
        self.assertNotIn("18612347060", report_text)
        self.assertNotIn("sig_mock_healthy_sign_abcdef1234567890abcdef1234567890abcdef12", report_text)
        self.assertNotIn("tok_mock_healthy_access_token_abcdef1234567890", report_text)
        self.assertNotIn("ref_mock_healthy_refresh_token_abcdef1234567890", report_text)

        # 存在脱敏标记
        self.assertIn("186****7060", report_text)

    # -------------------------------------------------------------------------
    # D16: to_dict() 机器可读格式校验
    # -------------------------------------------------------------------------
    def test_D16_to_dict_schema(self):
        """D16: report.to_dict() 输出合法的标准字典格式"""
        self._setup_healthy_environment()
        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)
        d = report.to_dict()

        self.assertIsInstance(d, dict)
        self.assertIn("timestamp", d)
        self.assertIn("doctor_id", d)
        self.assertIn("overall_status", d)
        self.assertIn("checks", d)
        self.assertIsInstance(d["checks"], list)
        self.assertGreater(len(d["checks"]), 5)

    # -------------------------------------------------------------------------
    # D17: 纯只读保证校验 (执行体检不改变任何现有文件与数据)
    # -------------------------------------------------------------------------
    def test_D17_pure_read_only_guarantee(self):
        """D17: 非破坏性只读验证：体检前后五大关键文件 SHA-256、大小与修改时间绝对一致，SQLite mode=ro 拒绝写入，临时探测文件被立即清理"""
        import hashlib
        self._setup_healthy_environment()

        def get_file_meta(file_path):
            h = hashlib.sha256()
            with open(file_path, "rb") as f:
                h.update(f.read())
            return {
                "sha256": h.hexdigest(),
                "size": os.path.getsize(file_path),
                "mtime": os.path.getmtime(file_path),
            }

        target_files = [
            self.cred_path,
            self.state_path,
            self.config_path,
            self.timetable_path,
            self.db_path,
        ]

        before_metas = {fp: get_file_meta(fp) for fp in target_files}

        doctor = SystemDoctor(root_dir=self.test_dir)
        report = doctor.run_diagnostics(online_probe=False)

        after_metas = {fp: get_file_meta(fp) for fp in target_files}

        # 1. 密码学哈希、大小与 mtime 全面比对，验证数据字节级绝对不可变
        for fp in target_files:
            fname = os.path.basename(fp)
            self.assertEqual(before_metas[fp]["sha256"], after_metas[fp]["sha256"], f"{fname} SHA-256 发生改变！")
            self.assertEqual(before_metas[fp]["size"], after_metas[fp]["size"], f"{fname} 大小发生改变！")
            self.assertEqual(before_metas[fp]["mtime"], after_metas[fp]["mtime"], f"{fname} mtime 发生改变！")

        # 2. 验证 SQLite mode=ro URI 只读连接拒绝任何写操作 (抛出 OperationalError)
        db_abs = os.path.abspath(self.db_path)
        ro_uri = f"file:{db_abs}?mode=ro"
        ro_conn = sqlite3.connect(ro_uri, uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                ro_conn.execute("INSERT INTO registration_records VALUES ('act_illegal', '只读探测', 'FAIL');")
            self.assertIn("readonly", str(ctx.exception).lower())
        finally:
            ro_conn.close()

        # 3. 确认没有残留探针临时文件
        for root, _, files in os.walk(self.test_dir):
            for fname in files:
                self.assertFalse(fname.startswith(".doctor_probe_"), f"存在未清理临时文件: {fname}")

    # -------------------------------------------------------------------------
    # D18: lejian.py doctor / --doctor 命令与 --json 参数验证
    # -------------------------------------------------------------------------
    def test_D18_cli_doctor_routing(self):
        """D18: 验证 cmd_doctor 支持文本报告输出与 --json 参数输出"""
        from lejian import cmd_doctor

        # 文本模式
        buf = io.StringIO()
        with patch.object(sys, "argv", ["lejian.py", "doctor", "--offline"]):
            with patch("sys.stdout", buf):
                cmd_doctor()
        out = buf.getvalue()
        self.assertIn("lejianPE 系统健康诊断报告", out)
        self.assertIn("总体健康评估", out)

        # JSON 模式
        buf_json = io.StringIO()
        with patch.object(sys, "argv", ["lejian.py", "--doctor", "--json", "--offline"]):
            with patch("sys.stdout", buf_json):
                cmd_doctor()
        out_json = buf_json.getvalue()
        parsed = json.loads(out_json)
        self.assertIn("overall_status", parsed)
        self.assertIn("checks", parsed)

    # -------------------------------------------------------------------------
    # D19: menu.py 动作路由验证
    # -------------------------------------------------------------------------
    def test_D19_menu_routing_contains_doctor(self):
        """D19: menu.py ACTIONS 中应包含 [d] 选项并指向 lejian.py doctor"""
        import menu
        action_map = {item[0]: item for item in menu.ACTIONS}
        self.assertIn("d", action_map)
        key, label, cmd = action_map["d"]
        self.assertIn("系统健康体检", label)
        self.assertEqual(cmd, ["lejian.py", "doctor"])


if __name__ == "__main__":
    unittest.main()
