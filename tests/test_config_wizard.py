"""
乐健体育 · 首次配置向导与环境自检自动化测试套件 (W01 ~ W23).
==============================================================
覆盖执行计划 M3 (第三阶段) 全部验收指标与加固设计：
1. detect_system_state 状态检测：
   - W01: NO_CONFIG (完全无配置)
   - W02: CRED_WITHOUT_STATE (有凭据无会话)
   - W03: READY (凭据与会话均有效)
   - W04: CRED_CORRUPTED (凭据 JSON 损坏)
   - W05: CRED_CORRUPTED (凭据缺少必要字段)
   - W06: TOKEN_EXPIRED_REFRESHABLE (Token 过期但 RefreshToken 存在)
   - W07: SESSION_EXPIRED (会话过期且无 RefreshToken)
   - W08: PENDING_TRANSACTION (上次初始化遗留未决事务)
2. 环境自检与格式化输出：
   - W09: check_environment 综合自检（Python、OS、目录权限、SQLite、ADB 探测）
3. 向导自动修复与差异化路由执行：
   - W10: PENDING_TRANSACTION 自动自愈回滚并平滑重试
   - W11: READY 状态跳过初始化直接返回成功
   - W12: TOKEN_EXPIRED_REFRESHABLE 静默自动刷新并原子落地
   - W13: 静默刷新失败自动平滑降级至密码快速登录
   - W14: CRED_WITHOUT_STATE 免抓包快速登录初始化会话
   - W15: 快速登录中密码错误自动诊断并保护凭据
   - W16: CRED_CORRUPTED 引导进入重新初始化
   - W17: NO_CONFIG 全新环境全流程开箱引导与落地
   - W18: 全流程敏感信息脱敏校验（绝不包含明文密码与完整 Token）
   - W19: lejian.py setup/wizard 与 menu.py 动作路由验证
   - W20: 快速登录遇到 50004 签名错误自动引导重新初始化闭环
   - W21: 静默刷新 Single-flight 单飞并发控制验证（防并发重复刷新与轮换冲突）
   - W22: 静默刷新失败冷却保护（防无限快速重试打爆服务端）
   - W23: 环境自检网络探测非阻断性验证（离线环境下仍放行本地查看）
"""
import io
import json
import os
import shutil
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

from core.auth_error import BusinessAuthError
from core.config_wizard import ConfigWizard, SystemSetupState
from core.credential_storage import CredentialStorage
from tests.network_guard import install_network_guard
from tools.login_envelope import wrap


class ConfigWizardTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_network_guard()

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_wizard_")
        self.cred_path = os.path.join(self.test_dir, "credential.json")
        self.state_path = os.path.join(self.test_dir, "state.json")
        self.flows_path = os.path.join(self.test_dir, "login_flows.jsonl")
        self.wizard = ConfigWizard(
            cred_path=self.cred_path,
            state_path=self.state_path,
            flows_path=self.flows_path,
            use_dpapi=False,  # 测试环境默认统一用非 DPAPI 方便 cross-platform
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_mock_flows(self, account="13800138000", sign_digital="sig_captured_999", entrance="1"):
        """构造合法的抓包 jsonl 文件"""
        payload = {
            "userName": account,
            "signDigital": sign_digital,
            "entrance": entrance,
        }
        env = wrap(payload)
        line = json.dumps({
            "status": 200,
            "req_body": json.dumps(env),
            "url": "https://cpes.legym.cn/authorization/user/v2/manage/login"
        })
        with open(self.flows_path, "w", encoding="utf-8") as f:
            f.write(line + "\n")

    # ==================== W01: NO_CONFIG 状态检测 ====================
    def test_W01_detect_state_no_config(self):
        """W01: 当没有 credential.json 时，系统应判定为 NO_CONFIG"""
        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.NO_CONFIG)
        self.assertTrue(res.get("can_reinit"))

    # ==================== W02: CRED_WITHOUT_STATE 状态检测 ====================
    def test_W02_detect_state_cred_without_state(self):
        """W02: 当有有效凭据但无 state.json 时，判定为 CRED_WITHOUT_STATE"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_123", use_dpapi=False)
        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.CRED_WITHOUT_STATE)
        self.assertEqual(res["account"], "13800138000")
        self.assertIn("138****8000", res["account_masked"])
        self.assertTrue(res.get("can_login"))

    # ==================== W03: READY 状态检测 ====================
    def test_W03_detect_state_ready(self):
        """W03: 当凭据存在且 state.json 有效、Token 未过期时，判定为 READY"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_123", use_dpapi=False)
        valid_state = {
            "accessToken": "tok_live_123",
            "refreshToken": "ref_live_123",
            "schoolId": "sch_live_123",
            "semesterId": "sem_live_123",
            "accessTokenExpires": int((time.time() + 7200) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(valid_state, f)

        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.READY)
        self.assertEqual(res["semester_id"], "sem_live_123")
        self.assertTrue(res.get("can_proceed"))

    # ==================== W04: CRED_CORRUPTED 损坏 JSON 状态检测 ====================
    def test_W04_detect_state_cred_corrupted_json(self):
        """W04: 凭据文件不是有效 JSON 时，判定为 CRED_CORRUPTED"""
        with open(self.cred_path, "w", encoding="utf-8") as f:
            f.write("{bad_json: invalid...")

        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.CRED_CORRUPTED)
        self.assertIn("损坏", res["details"])
        self.assertTrue(res.get("can_reinit"))

    # ==================== W05: CRED_CORRUPTED 缺失字段状态检测 ====================
    def test_W05_detect_state_cred_missing_fields(self):
        """W05: 凭据缺失 signDigital 字段时，判定为 CRED_CORRUPTED"""
        with open(self.cred_path, "w", encoding="utf-8") as f:
            json.dump({"account": "13800138000"}, f)

        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.CRED_CORRUPTED)
        self.assertTrue(res.get("can_reinit"))

    # ==================== W06: TOKEN_EXPIRED_REFRESHABLE 状态检测 ====================
    def test_W06_detect_state_token_expired_refreshable(self):
        """W06: Token 已过期但存在 refreshToken 时，判定为 TOKEN_EXPIRED_REFRESHABLE"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_123", use_dpapi=False)
        expired_state = {
            "accessToken": "tok_old_expired",
            "refreshToken": "ref_still_valid",
            "schoolId": "sch_123",
            "semesterId": "sem_123",
            "accessTokenExpires": int((time.time() - 3600) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(expired_state, f)

        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.TOKEN_EXPIRED_REFRESHABLE)
        self.assertTrue(res.get("can_refresh"))

    # ==================== W07: SESSION_EXPIRED 状态检测 ====================
    def test_W07_detect_state_session_expired(self):
        """W07: Token 已过期且缺失 refreshToken 时，判定为 SESSION_EXPIRED"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_123", use_dpapi=False)
        no_ref_state = {
            "accessToken": "tok_old_expired",
            "refreshToken": "",
            "schoolId": "sch_123",
            "semesterId": "sem_123",
            "accessTokenExpires": int((time.time() - 3600) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(no_ref_state, f)

        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.SESSION_EXPIRED)
        self.assertTrue(res.get("can_login"))

    # ==================== W08: PENDING_TRANSACTION 状态检测 ====================
    def test_W08_detect_state_pending_transaction(self):
        """W08: 存在未提交的事务记录时，优先判定为 PENDING_TRANSACTION"""
        tx_data = {
            "tx_id": "tx_uncommitted_123",
            "status": "CRED_APPLIED",
        }
        with open(self.wizard.initializer.tx_path, "w", encoding="utf-8") as f:
            json.dump(tx_data, f)

        res = self.wizard.detect_system_state()
        self.assertEqual(res["state"], SystemSetupState.PENDING_TRANSACTION)
        self.assertTrue(res.get("can_auto_recover"))

    # ==================== W09: check_environment 综合自检 ====================
    def test_W09_environment_check_comprehensive(self):
        """W09: 验证环境自检全面检查 Python、OS、目录权限与 SQLite"""
        env_res = self.wizard.check_environment()
        self.assertTrue(env_res["python_ok"])
        self.assertTrue(env_res["dirs_ok"])
        self.assertTrue(env_res["sqlite_ok"])
        self.assertTrue(env_res["overall_ok"])

        report = self.wizard.format_environment_report(env_res)
        self.assertIn("Python 版本", report)
        self.assertIn("SQLite 驱动", report)
        self.assertIn("目录读写权限", report)

    # ==================== W10: 向导处理 PENDING_TRANSACTION 自动自愈 ====================
    def test_W10_wizard_pending_transaction_auto_heal(self):
        """W10: 向导检测到未决事务时，自动恢复旧凭据与会话，并无缝进入后续分支"""
        # 预设旧凭据备份与未决事务
        old_cred = {"account": "13800138000", "signDigital": "sig_orig_backup", "entrance": "1"}
        with open(self.wizard.initializer.cred_tx_bak, "w", encoding="utf-8") as f:
            json.dump(old_cred, f)
        # 预设当前被污染的凭据
        with open(self.cred_path, "w", encoding="utf-8") as f:
            json.dump({"account": "13800138000", "signDigital": "bad_sig"}, f)
        # 未决事务文件
        tx_data = {
            "tx_id": "tx_uncommitted",
            "status": "CRED_APPLIED",
            "cred_path": self.cred_path,
            "cred_had_existed": True,
            "cred_tx_bak": self.wizard.initializer.cred_tx_bak,
            "state_path": self.state_path,
            "state_had_existed": False,
            "state_tx_bak": None,
        }
        with open(self.wizard.initializer.tx_path, "w", encoding="utf-8") as f:
            json.dump(tx_data, f)

        stdout_buf = io.StringIO()
        # 自愈后状态应变为 CRED_WITHOUT_STATE，模拟输入密码快速登录成功
        mock_login = {
            "accessToken": "tok_auto_heal_123",
            "refreshToken": "ref_auto_heal_123",
            "schoolId": "sch_heal",
        }
        with patch.object(self.wizard.auth_manager, "login", return_value=mock_login), \
             patch.object(self.wizard.auth_manager, "fetch_semester_info", return_value=("sem_heal", "2026", None)), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.run_wizard(interactive=False, password="Password123")
            self.assertTrue(ok)

        out = stdout_buf.getvalue()
        self.assertIn("自动回滚并清理未决事务", out)
        # 凭据已恢复成旧凭据
        loaded = self.wizard.initializer.storage.load_credential()
        self.assertEqual(loaded["signDigital"], "sig_orig_backup")
        # 事务文件已被清理
        self.assertFalse(os.path.exists(self.wizard.initializer.tx_path))

    # ==================== W11: READY 状态跳过初始化直接返回成功 ====================
    def test_W11_wizard_ready_skips_initialization(self):
        """W11: 当系统状态为 READY 时，向导直接输出就绪信息并返回 True，无需重复初始化"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_123", use_dpapi=False)
        valid_state = {
            "accessToken": "tok_ready",
            "refreshToken": "ref_ready",
            "schoolId": "sch_ready",
            "semesterId": "sem_ready",
            "accessTokenExpires": int((time.time() + 3600) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(valid_state, f)

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            ok = self.wizard.run_wizard(interactive=True)
            self.assertTrue(ok)

        out = stdout_buf.getvalue()
        self.assertIn("系统配置已就绪", out)
        self.assertIn("138****8000", out)
        self.assertIn("sem_ready", out)

    # ==================== W12: TOKEN_EXPIRED_REFRESHABLE 静默刷新成功 ====================
    def test_W12_wizard_token_expired_silent_refresh_success(self):
        """W12: Token 过期但 RefreshToken 存在时，向导自动静默刷新并更新 state.json"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_123", use_dpapi=False)
        expired_state = {
            "accessToken": "tok_old",
            "refreshToken": "ref_valid_token",
            "schoolId": "sch_123",
            "semesterId": "sem_123",
            "accessTokenExpires": int((time.time() - 100) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(expired_state, f)

        stdout_buf = io.StringIO()
        # Mock silent_refresh_token 返回成功
        with patch.object(self.wizard, "silent_refresh_token", return_value=(True, "2026-11-01 23:59:59")), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.run_wizard(interactive=True)
            self.assertTrue(ok)

        out = stdout_buf.getvalue()
        self.assertIn("Token 自动刷新成功", out)

    # ==================== W13: 静默刷新失败平滑降级至密码快速登录 ====================
    def test_W13_wizard_token_expired_refresh_failure_falls_back_to_login(self):
        """W13: 静默刷新被服务端拒绝时，平滑降级为免抓包的快速登录并成功初始化"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_123", use_dpapi=False)
        expired_state = {
            "accessToken": "tok_old",
            "refreshToken": "ref_revoked",
            "schoolId": "sch_123",
            "semesterId": "sem_123",
            "accessTokenExpires": int((time.time() - 100) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(expired_state, f)

        mock_login = {
            "accessToken": "tok_new_from_fallback",
            "refreshToken": "ref_new_from_fallback",
            "schoolId": "sch_123",
        }

        stdout_buf = io.StringIO()
        with patch.object(self.wizard, "silent_refresh_token", return_value=(False, "HTTP 401 凭据失效")), \
             patch.object(self.wizard.auth_manager, "login", return_value=mock_login), \
             patch.object(self.wizard.auth_manager, "fetch_semester_info", return_value=("sem_fallback", "2026", None)), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.run_wizard(interactive=False, password="MyPassword888")
            self.assertTrue(ok)

        out = stdout_buf.getvalue()
        self.assertIn("正在转入快速登录流程", out)
        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["accessToken"], "tok_new_from_fallback")

    # ==================== W14: CRED_WITHOUT_STATE 免抓包快速登录成功 ====================
    def test_W14_wizard_cred_without_state_quick_login_success(self):
        """W14: 有有效凭据但无会话时，无需抓包，仅输入密码即完成登录并生成 state.json"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_valid_quick", use_dpapi=False)

        mock_login = {
            "accessToken": "tok_quick_login",
            "refreshToken": "ref_quick_login",
            "schoolId": "sch_quick",
        }

        stdout_buf = io.StringIO()
        with patch.object(self.wizard.auth_manager, "login", return_value=mock_login), \
             patch.object(self.wizard.auth_manager, "fetch_semester_info", return_value=("sem_quick", "2026", None)), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.quick_login_with_existing_credential(password="Pass12345", interactive=False)
            self.assertTrue(ok)

        out = stdout_buf.getvalue()
        self.assertIn("会话初始化完成", out)
        self.assertIn("138****8000", out)

        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["accessToken"], "tok_quick_login")
        self.assertEqual(st["semesterId"], "sem_quick")

    # ==================== W15: 快速登录密码错误诊断与凭据保护 ====================
    def test_W15_wizard_cred_without_state_wrong_password_diagnostic(self):
        """W15: 快速登录遇到密码错误时输出结构化诊断，并完整保留旧凭据不受损"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_safe_orig", use_dpapi=False)

        stdout_buf = io.StringIO()
        with patch.object(self.wizard.auth_manager, "login", side_effect=BusinessAuthError("密码错误", code=1001)), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.quick_login_with_existing_credential(password="WrongPass", interactive=False)
            self.assertFalse(ok)

        out = stdout_buf.getvalue()
        self.assertIn("登录失败", out)
        self.assertIn("账号或密码不正确", out)

        # 凭据丝毫未损
        loaded = self.wizard.initializer.storage.load_credential()
        self.assertEqual(loaded["signDigital"], "sig_safe_orig")

    # ==================== W16: CRED_CORRUPTED 引导进入重新初始化 ====================
    def test_W16_wizard_cred_corrupted_prompts_reinit(self):
        """W16: 凭据文件损坏时，向导自动提示并引导进入重新初始化"""
        with open(self.cred_path, "w", encoding="utf-8") as f:
            f.write("corrupted content")

        stdout_buf = io.StringIO()
        with patch.object(self.wizard.initializer, "reinitialize", return_value=True) as mock_reinit, \
             patch("builtins.input", return_value="1"), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.run_wizard(interactive=True, password="Pass")
            self.assertTrue(ok)
            mock_reinit.assert_called_once()

        out = stdout_buf.getvalue()
        self.assertIn("本地凭据损坏警告", out)

    # ==================== W17: NO_CONFIG 全新环境全流程开箱引导与落地 ====================
    def test_W17_wizard_no_config_full_onboarding_success(self):
        """W17: 全新环境下走通完整首次配置向导，生成 credential.json 与 state.json"""
        self._create_mock_flows("13900139000", "sig_new_onboarding", "1")

        mock_login = {
            "accessToken": "tok_onboarding",
            "refreshToken": "ref_onboarding",
            "schoolId": "sch_onboard",
        }

        stdout_buf = io.StringIO()
        with patch.object(self.wizard.auth_manager, "login", return_value=mock_login), \
             patch.object(self.wizard.auth_manager, "fetch_semester_info", return_value=("sem_onboard", "2026", None)), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.run_wizard(
                interactive=False,
                account="13900139000",
                password="MyCorrectPassword",
            )
            self.assertTrue(ok)

        # 检查凭据与会话落盘
        loaded_cred = self.wizard.initializer.storage.load_credential()
        self.assertEqual(loaded_cred["account"], "13900139000")
        self.assertEqual(loaded_cred["signDigital"], "sig_new_onboarding")

        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["accessToken"], "tok_onboarding")
        self.assertEqual(st["semesterId"], "sem_onboard")

    # ==================== W18: 全流程敏感信息脱敏校验 ====================
    def test_W18_wizard_security_masking_no_plaintext(self):
        """W18: 验证向导全流程在终端输出中绝不泄漏密码明文与完整 Token"""
        secret_pwd = "VerySecretWizardPassword999!"
        self._create_mock_flows("13800138000", "sig_secret_token_123456", "1")

        mock_login = {
            "accessToken": "tok_secret_jwt_stream_xxx",
            "refreshToken": "ref_secret_jwt_stream_yyy",
            "schoolId": "sch_123",
        }

        stdout_buf = io.StringIO()
        with patch.object(self.wizard.auth_manager, "login", return_value=mock_login), \
             patch.object(self.wizard.auth_manager, "fetch_semester_info", return_value=("sem_sec", "2026", None)), \
             patch("sys.stdout", stdout_buf):
            self.wizard.run_wizard(
                interactive=False,
                account="13800138000",
                password=secret_pwd,
            )

        out = stdout_buf.getvalue()
        self.assertNotIn(secret_pwd, out, "密码明文泄漏在向导终端输出中！")
        self.assertNotIn("tok_secret_jwt_stream_xxx", out, "完整 Token 泄漏在向导终端输出中！")

    # ==================== W19: lejian.py setup/wizard 与 menu.py 动作路由验证 ====================
    def test_W19_menu_and_lejian_setup_routing(self):
        """W19: 验证 lejian.py setup/wizard 命令及 menu.py 动作路由正确绑定到 cmd_setup"""
        import lejian
        import menu

        # 验证 menu.py 中新增的 [s] 配置向导入口
        hit_s = next((a for a in menu.ACTIONS if a[0] == "s"), None)
        self.assertIsNotNone(hit_s, "menu.py 中缺少 [s] 配置向导选项")
        self.assertEqual(hit_s[2], ["lejian.py", "setup"])

        # 验证 lejian.py 能够成功调用 cmd_setup
        with patch("core.config_wizard.ConfigWizard.run_wizard", return_value=True) as mock_wizard:
            lejian.cmd_setup()
            mock_wizard.assert_called_once()

    # ==================== W20: 快速登录遇到 50004 签名错误引导闭环 ====================
    def test_W20_quick_login_50004_signature_error_prompts_reinit_loop(self):
        """W20: 快速登录遇到 50004 签名失效时（如改了密码），自动引导进入重新初始化闭环"""
        self.wizard.initializer.storage.save_credential("13800138000", "sig_outdated", use_dpapi=False)

        stdout_buf = io.StringIO()
        with patch.object(self.wizard.auth_manager, "login", side_effect=BusinessAuthError("50004 签名校验失败", code=50004)), \
             patch.object(self.wizard.initializer, "reinitialize", return_value=True) as mock_reinit, \
             patch("builtins.input", return_value="1"), \
             patch("sys.stdout", stdout_buf):
            ok = self.wizard.quick_login_with_existing_credential(password="Pass123", interactive=True)
            self.assertTrue(ok)
            mock_reinit.assert_called_once()

        out = stdout_buf.getvalue()
        self.assertIn("闭环引导", out)
        self.assertIn("当前凭据的签名可能已失效", out)

    # ==================== W21: 静默刷新 Single-flight 单飞并发控制验证 ====================
    def test_W21_silent_refresh_single_flight_concurrency(self):
        """W21: 多个并发请求同时发现 Token 过期时，Single-flight 机制确保仅发起 1 次刷新请求，避免轮换冲突"""
        import threading

        ConfigWizard._last_failure_time = 0.0
        ConfigWizard._last_failure_reason = ""

        # 预设过期的 state.json
        state_data = {
            "accessToken": "tok_expired_old",
            "refreshToken": "ref_valid_token",
            "schoolId": "sch_123",
            "semesterId": "sem_123",
            "accessTokenExpires": int((time.time() - 100) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f)

        # 统计向服务端发起真实 HTTP 请求的次数
        http_call_count = [0]
        original_urlopen = urllib.request.urlopen

        class MockHttpResponse:
            def __init__(self, data_bytes):
                self.status = 200
                self._data = data_bytes

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        def mock_urlopen_fn(req, *args, **kwargs):
            if "refreshToken" in str(getattr(req, "full_url", req)):
                http_call_count[0] += 1
                # 模拟轻微网络耗时，放大并发重叠
                time.sleep(0.05)
                resp_payload = {
                    "code": 0,
                    "data": {
                        "accessToken": "tok_singleflight_fresh_999",
                        "refreshToken": "ref_rotated_new_token_888",
                        "accessTokenExpires": int((time.time() + 3600) * 1000),
                    }
                }
                return MockHttpResponse(json.dumps(resp_payload).encode("utf-8"))
            return original_urlopen(req, *args, **kwargs)

        threads = []
        results = [None, None, None]

        def worker(idx):
            res_ok, res_str = self.wizard.silent_refresh_token()
            results[idx] = (res_ok, res_str)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_fn):
            for i in range(3):
                t = threading.Thread(target=worker, args=(i,))
                threads.append(t)
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # 核心断言：3 个并发请求全部刷新成功！
        for i in range(3):
            self.assertTrue(results[i][0], f"线程 {i} 刷新未能成功")

        # 核心断言：向服务端实际仅发起了 1 次网络调用（Single-flight 成功避免了重复刷新与轮换冲突）！
        self.assertEqual(http_call_count[0], 1, f"并发请求没有合并为单飞，实际触发了 {http_call_count[0]} 次请求！")

        # 检查磁盘会话已经更新为最新的 Token
        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["accessToken"], "tok_singleflight_fresh_999")
        self.assertEqual(st["refreshToken"], "ref_rotated_new_token_888")

    # ==================== W22: 静默刷新失败冷却保护 ====================
    def test_W22_silent_refresh_cooldown_prevents_infinite_retries(self):
        """W22: 静默刷新失败后触发冷却保护，短时间内的重试直接被拦截，防止打满服务端接口"""
        ConfigWizard._last_failure_time = 0.0
        ConfigWizard._last_failure_reason = ""

        state_data = {
            "accessToken": "tok_old",
            "refreshToken": "ref_bad_token",
            "accessTokenExpires": int((time.time() - 100) * 1000),
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state_data, f)

        # 模拟第一次刷新遭遇 401
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)):
            ok1, err1 = self.wizard.silent_refresh_token()
            self.assertFalse(ok1)
            self.assertIn("401", err1)

        # 紧接着第二次刷新（处于冷却期内）
        # 哪怕这次 mock 成功，也会因为处于冷却保护直接返回冷却提示，绝不发起多余调用
        with patch("urllib.request.urlopen") as mock_url:
            ok2, err2 = self.wizard.silent_refresh_token()
            self.assertFalse(ok2)
            self.assertIn("刷新冷却保护中", err2)
            mock_url.assert_not_called()

        # 重置冷却时间，避免污染后续测试
        ConfigWizard._last_failure_time = 0.0
        ConfigWizard._last_failure_reason = ""

    # ==================== W23: 环境自检网络探测非阻断性验证 ====================
    def test_W23_environment_check_network_offline_non_blocking(self):
        """W23: 模拟网络断网/DNS 解析失败/超时，验证报告正确记录网络警告，但 overall_ok 仍为 True（非阻断）"""
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("DNS resolution failed (Offline)")):
            env_res = self.wizard.check_environment()
            self.assertFalse(env_res["network_ok"])
            self.assertIn("受限或未连接", env_res["network_desc"])
            # 关键验证：网络不可用不会阻断本地基本功能的查看！
            self.assertTrue(env_res["overall_ok"], "网络异常不应导致 overall_ok 失败（必须是非阻断式检测）")

        report = self.wizard.format_environment_report(env_res)
        self.assertIn("网络连通性", report)
        self.assertIn("受限或未连接", report)


if __name__ == "__main__":
    unittest.main()
