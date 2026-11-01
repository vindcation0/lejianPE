"""
乐健体育 · 认证模块与会话管理自动化测试套件 (A01 ~ A20)。
"""
import base64
import copy
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import lejian
from core.auth_error import (
    AuthError,
    AuthNetworkError,
    BusinessAuthError,
    CredentialError,
    InvalidCredentialError,
    SchoolInfoError,
    SemesterInfoError,
    TokenAcquisitionError,
)
from core.auth_manager import AuthManager
from core.credential_storage import (
    CredentialStorage,
    dpapi_protect,
    dpapi_unprotect,
    is_windows,
    mask_account,
    mask_secret,
)
from tools.login_envelope import dynamic_key, unwrap, wrap


class AuthTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import socket
        cls._orig_socket_connect = socket.socket.connect
        def guarded_connect(self, address):
            raise RuntimeError(f"测试隔离屏障触发：单元测试严禁发起外部真实网络连接！目标: {address}")
        socket.socket.connect = guarded_connect

    @classmethod
    def tearDownClass(cls):
        import socket
        socket.socket.connect = cls._orig_socket_connect

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_auth_")
        self.cred_path = os.path.join(self.test_dir, "credential.json")
        self.state_path = os.path.join(self.test_dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ==================== A01: 正常手动密码输入 ====================
    def test_A01_normal_manual_password_login(self):
        auth_mgr = AuthManager(ssl_verify=False)
        mock_resp_data = {
            "accessToken": "tok_test_12345",
            "refreshToken": "ref_test_67890",
            "accessTokenExpires": int(time.time() * 1000) + 7200000,
            "schoolId": "sch_001",
            "semesterId": "sem_001",
            "realName": "张三",
        }
        mock_body = json.dumps({"code": 0, "message": "OK", "data": mock_resp_data})

        with patch.object(auth_mgr, "post_json", return_value=(200, mock_body)):
            res = auth_mgr.login("13800138000", "my_secret_pass", "signdigital_abc", "1")
            self.assertEqual(res["accessToken"], "tok_test_12345")
            self.assertEqual(res["schoolId"], "sch_001")
            self.assertEqual(res["realName"], "张三")

    # ==================== A02: 特殊字符密码 ====================
    def test_A02_special_character_password(self):
        special_passwords = [
            "P@ssw0rd!#%^&*()_+~|}{[]:;?><,./'",
            "中文密码测试￥…*（）",
            "   leading_and_trailing_spaces   ",
            "emoji_🔑_🔐_pass",
        ]
        for sp in special_passwords:
            payload = {
                "userName": "13800000000",
                "password": sp,
                "signDigital": "sig_special_test",
                "entrance": "1",
            }
            env = wrap(payload)
            unwrapped = unwrap(env)
            self.assertEqual(unwrapped["password"], sp, f"密码编解码不一致: {sp}")

    # ==================== A03: 错误密码 ====================
    def test_A03_wrong_password_business_failure(self):
        auth_mgr = AuthManager(ssl_verify=False)
        mock_body = json.dumps({"code": 10002, "message": "密码错误，请重新输入"})

        with patch.object(auth_mgr, "post_json", return_value=(200, mock_body)):
            with self.assertRaises(BusinessAuthError) as ctx:
                auth_mgr.login("13800138000", "wrong_pass", "signdigital_abc", "1")
            self.assertIn("密码", str(ctx.exception))

    # ==================== A04: credential.json 缺失 ====================
    def test_A04_credential_missing(self):
        storage = CredentialStorage(os.path.join(self.test_dir, "nonexistent.json"))
        with self.assertRaises(CredentialError) as ctx:
            storage.load_credential()
        self.assertIn("未找到凭据文件", str(ctx.exception))

    # ==================== A05: credential.json 损坏 ====================
    def test_A05_credential_corrupted(self):
        with open(self.cred_path, "w", encoding="utf-8") as f:
            f.write("{invalid_json_content, missing_brackets")

        storage = CredentialStorage(self.cred_path)
        with self.assertRaises(InvalidCredentialError) as ctx:
            storage.load_credential()
        self.assertIn("损坏", str(ctx.exception))

    # ==================== A06: signDigital 缺失 ====================
    def test_A06_sign_digital_missing(self):
        with open(self.cred_path, "w", encoding="utf-8") as f:
            json.dump({"account": "13800138000", "entrance": "1"}, f)

        storage = CredentialStorage(self.cred_path)
        with self.assertRaises(InvalidCredentialError) as ctx:
            storage.load_credential()
        self.assertIn("signDigital", str(ctx.exception))

    # ==================== A07: 密码修改后认证材料失效 ====================
    def test_A07_password_changed_signature_invalid_50004(self):
        auth_mgr = AuthManager(ssl_verify=False)
        mock_body = json.dumps({"code": 50004, "message": "签名校验失败，请重新登录"})

        with patch.object(auth_mgr, "post_json", return_value=(200, mock_body)):
            with self.assertRaises(BusinessAuthError) as ctx:
                auth_mgr.login("13800138000", "new_password", "old_sign_digital", "1")
            self.assertEqual(ctx.exception.code, 50004)
            self.assertIn("签名", str(ctx.exception))

    # ==================== A08: HTTP 200 业务失败 ====================
    def test_A08_http_200_business_failure(self):
        auth_mgr = AuthManager(ssl_verify=False)
        mock_body = json.dumps({"code": 40001, "message": "当前账号已被冻结"})

        with patch.object(auth_mgr, "post_json", return_value=(200, mock_body)):
            with self.assertRaises(BusinessAuthError) as ctx:
                auth_mgr.login("13800138000", "pass123", "sig_abc", "1")
            self.assertEqual(ctx.exception.code, 40001)
            self.assertIn("冻结", str(ctx.exception))

    # ==================== A09: Token 获取失败 ====================
    def test_A09_token_acquisition_failure(self):
        auth_mgr = AuthManager(ssl_verify=False)
        mock_body = json.dumps({
            "code": 0,
            "message": "OK",
            "data": {"schoolId": "sch_001", "id": "user_001"}  # 缺少 accessToken
        })

        with patch.object(auth_mgr, "post_json", return_value=(200, mock_body)):
            with self.assertRaises(TokenAcquisitionError) as ctx:
                auth_mgr.login("13800138000", "pass123", "sig_abc", "1")
            self.assertIn("accessToken", str(ctx.exception))

    # ==================== A10: schoolId 获取失败 ====================
    def test_A10_school_id_missing(self):
        auth_mgr = AuthManager(ssl_verify=False)
        mock_body = json.dumps({
            "code": 0,
            "message": "OK",
            "data": {
                "accessToken": "tok_123",
                "refreshToken": "ref_123",
                "id": "user_001"
                # 缺少 schoolId
            }
        })

        with patch.object(auth_mgr, "post_json", return_value=(200, mock_body)):
            with self.assertRaises(SchoolInfoError) as ctx:
                auth_mgr.login("13800138000", "pass123", "sig_abc", "1")
            self.assertIn("schoolId", str(ctx.exception))

    # ==================== A11: semesterId 获取失败 ====================
    def test_A11_semester_id_missing_protects_state(self):
        auth_mgr = AuthManager(ssl_verify=False)
        login_data = {
            "accessToken": "tok_123",
            "refreshToken": "ref_123",
            "schoolId": "sch_001",
            "id": "user_001",
        }
        # mock fetch_semester_info 返回 None
        with patch.object(auth_mgr, "fetch_semester_info", return_value=(None, None, "拉取失败")):
            with self.assertRaises(SemesterInfoError):
                auth_mgr.save_state_atomically(login_data, self.state_path)

        # 确保没有落盘残损的 state.json
        self.assertFalse(os.path.exists(self.state_path))

    # ==================== A12: 登录失败不覆盖旧 state ====================
    def test_A12_login_save_failure_preserves_old_state(self):
        auth_mgr = AuthManager(ssl_verify=False)
        original_state = {
            "accessToken": "original_valid_token",
            "refreshToken": "original_valid_refresh",
            "schoolId": "orig_sch",
            "semesterId": "orig_sem",
            "year": "2026",
            "realName": "原有用户",
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(original_state, f)

        # 尝试保存缺少 semesterId 的异常会话
        bad_login_data = {
            "accessToken": "new_tok",
            "refreshToken": "new_ref",
            "schoolId": "new_sch",
        }
        with patch.object(auth_mgr, "fetch_semester_info", return_value=(None, None, "拉取失败")):
            with self.assertRaises(SemesterInfoError):
                auth_mgr.save_state_atomically(bad_login_data, self.state_path)

        # 验证旧 state.json 完好无损
        with open(self.state_path, "r", encoding="utf-8") as f:
            current_state = json.load(f)
        self.assertEqual(current_state["accessToken"], "original_valid_token")
        self.assertEqual(current_state["realName"], "原有用户")

    # ==================== A13: 日志敏感信息脱敏 ====================
    def test_A13_log_masking(self):
        phone = "13800138000"
        masked_phone = mask_account(phone)
        self.assertEqual(masked_phone, "138****8000")
        self.assertNotIn("0013", masked_phone)

        token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        masked_tok = mask_secret(token)
        self.assertTrue(masked_tok.startswith("aaaa...eeee"))
        self.assertIn("长度=36", masked_tok)
        self.assertNotIn("bbbb-cccc", masked_tok)

        sign = "1111222233334444555566667777888899990000"
        masked_sign = mask_secret(sign)
        self.assertTrue(masked_sign.startswith("1111...0000"))
        self.assertIn("长度=40", masked_sign)
        self.assertNotIn("44445555", masked_sign)

    # ==================== A14: AES 信封本地测试 ====================
    def test_A14_envelope_roundtrip(self):
        for _ in range(50):
            t = int(time.time() * 1000)
            payload = {
                "user": "test_user",
                "random": os.urandom(8).hex(),
                "ts": t,
            }
            env = wrap(payload, t=t)
            self.assertIn("t", env)
            self.assertIn("pyd", env)
            recovered = unwrap(env)
            self.assertEqual(recovered, payload)

    # ==================== A15: 时间戳格式测试 ====================
    def test_A15_timestamp_validation(self):
        # 13 位毫秒时间戳正常派生
        t_valid = 1730438000000
        key = dynamic_key(t_valid)
        self.assertEqual(len(key), 16)

        # 非 13 位时间戳拒绝
        with self.assertRaises(ValueError):
            dynamic_key(1730438000)  # 10 位秒级时间戳
        with self.assertRaises(ValueError):
            dynamic_key(1730438000000123)  # 16 位微秒级时间戳

    # ==================== A16: AccessToken 自动刷新 ====================
    def test_A16_ensure_token_auto_refresh(self):
        expired_state = {
            "accessToken": "old_expired_tok",
            "refreshToken": "valid_refresh_tok",
            "accessTokenExpires": int(time.time() * 1000) - 10000,  # 已过期
            "schoolId": "sch_001",
        }
        client = lejian.Client(expired_state)

        new_expires = int(time.time() * 1000) + 7200000
        mock_refresh_resp = json.dumps({
            "code": 0,
            "data": {
                "accessToken": "new_fresh_token",
                "refreshToken": "new_rotated_refresh",
                "accessTokenExpires": new_expires,
            }
        })

        with patch("lejian._request", return_value=(200, mock_refresh_resp)), \
             patch("lejian.save_json"):
            tok = client.ensure_token(margin=1200)
            self.assertEqual(tok, "new_fresh_token")
            self.assertEqual(client.s["accessToken"], "new_fresh_token")

    # ==================== A17: RefreshToken 轮换 ====================
    def test_A17_refresh_token_rotation(self):
        state = {
            "accessToken": "tok_1",
            "refreshToken": "old_refresh_token",
            "accessTokenExpires": int(time.time() * 1000) + 100,
            "schoolId": "sch_001",
        }
        client = lejian.Client(state)

        mock_refresh_resp = json.dumps({
            "code": 0,
            "data": {
                "accessToken": "tok_2",
                "refreshToken": "new_rotated_refresh_token",
                "accessTokenExpires": int(time.time() * 1000) + 7200000,
            }
        })

        with patch("lejian._request", return_value=(200, mock_refresh_resp)), \
             patch("lejian.save_json") as mock_save:
            client.refresh()
            self.assertEqual(client.s["refreshToken"], "new_rotated_refresh_token")
            mock_save.assert_called_once()

    # ==================== A18: RefreshToken 失效 ====================
    def test_A18_refresh_token_expired(self):
        state = {
            "accessToken": "tok_1",
            "refreshToken": "expired_refresh_token",
            "accessTokenExpires": int(time.time() * 1000) - 100,
            "schoolId": "sch_001",
        }
        client = lejian.Client(state)

        mock_fail_resp = json.dumps({"code": 401, "message": "RefreshToken has expired"})
        with patch("lejian._request", return_value=(401, mock_fail_resp)):
            with self.assertRaises(RuntimeError) as ctx:
                client.refresh()
            self.assertIn("刷新失败", str(ctx.exception))

    # ==================== A19: 模拟器备用方式正常 ====================
    def test_A19_resync_from_app_compatibility(self):
        # 验证模拟器同步产生的字段与 Client 要求 100% 契合
        simulated_resync_state = {
            "accessToken": "app_sync_access_token",
            "refreshToken": "app_sync_refresh_token",
            "accessTokenExpires": int(time.time() * 1000) + 7200000,
            "userId": "user_app_001",
            "schoolId": "sch_app_001",
            "semesterId": "sem_app_001",
            "year": "2026",
            "mobile": "13800138000",
            "realName": "测试李四",
            "organizationUserNumber": "99999999",
            "campusId": "cam_001",
        }
        client = lejian.Client(simulated_resync_state)
        self.assertGreater(client.expires_in(), 0)
        self.assertEqual(client.s["schoolId"], "sch_app_001")
        self.assertEqual(client.s["semesterId"], "sem_app_001")

    # ==================== A20: 两种认证方式生成兼容会话 ====================
    def test_A20_both_auth_methods_generate_compatible_state(self):
        auth_mgr = AuthManager(ssl_verify=False)
        cli_login_data = {
            "accessToken": "cli_tok",
            "refreshToken": "cli_ref",
            "accessTokenExpires": int(time.time() * 1000) + 7200000,
            "id": "uid_001",
            "schoolId": "sch_001",
            "realName": "王五",
            "mobile": "13800000000",
            "campusId": "cam_001",
        }
        with patch.object(auth_mgr, "fetch_semester_info", return_value=("sem_001", "2026-2027", None)):
            saved_cli_state = auth_mgr.save_state_atomically(cli_login_data, self.state_path)

        # 检查关键字段完整性
        required_keys = [
            "accessToken", "refreshToken", "accessTokenExpires",
            "userId", "schoolId", "semesterId", "year"
        ]
        for k in required_keys:
            self.assertIn(k, saved_cli_state, f"CLI 状态缺少关键字段: {k}")

        # 使用 Client 加载会话并验证行为
        client = lejian.Client(saved_cli_state)
        self.assertGreater(client.expires_in(), 0)
        self.assertEqual(client.s["schoolId"], "sch_001")
        self.assertEqual(client.s["semesterId"], "sem_001")

    # ==================== 额外: Windows DPAPI 凭据加解密保护 ====================
    def test_dpapi_credential_protection_roundtrip(self):
        if not is_windows():
            self.skipTest("仅在 Windows 平台验证 DPAPI")
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800000000", "secret_sign_12345", "1", use_dpapi=True)
        
        # 确认落盘的 JSON 中不包含明文 signDigital
        with open(self.cred_path, "r", encoding="utf-8") as f:
            raw_content = json.load(f)
        self.assertNotIn("signDigital", raw_content)
        self.assertIn("signDigitalEncrypted", raw_content)

        # 读取时能透明无缝解密
        loaded = storage.load_credential()
        self.assertEqual(loaded["signDigital"], "secret_sign_12345")
        self.assertTrue(loaded["isEncrypted"])

    # ==================== A21: CLI 密码输入 (正确调用 getpass) ====================
    def test_A21_cli_password_input_uses_getpass(self):
        import tools.login_cli as login_cli
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "dummy_sig_123", "1", use_dpapi=False)

        mock_login_data = {
            "accessToken": "tok_cli_test",
            "refreshToken": "ref_cli_test",
            "accessTokenExpires": int(time.time() * 1000) + 7200000,
            "schoolId": "sch_001",
            "semesterId": "sem_001",
        }

        buf = io.StringIO()
        with patch("tools.login_cli.CRED_FILE", self.cred_path), \
             patch("tools.login_cli.STATE_FILE", self.state_path), \
             patch("getpass.getpass", return_value="SecretPass_A21") as mock_getpass, \
             patch("core.auth_manager.AuthManager.login", return_value=mock_login_data) as mock_login, \
             patch("core.auth_manager.AuthManager.save_state_atomically"), \
             patch("sys.stdout", buf):
            with patch("sys.argv", ["login_cli.py"]):
                login_cli.main()
            mock_getpass.assert_called_once()
            self.assertIn("密码", mock_getpass.call_args[0][0])
            self.assertEqual(mock_login.call_args[1]["password"], "SecretPass_A21")

    # ==================== A22: 密码不进入日志 (输出及日志不包含密码) ====================
    def test_A22_password_not_in_logs_or_stdout(self):
        import io
        import tools.login_cli as login_cli
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "dummy_sig_123", "1", use_dpapi=False)

        mock_login_data = {
            "accessToken": "tok_cli_test",
            "refreshToken": "ref_cli_test",
            "accessTokenExpires": int(time.time() * 1000) + 7200000,
            "schoolId": "sch_001",
            "semesterId": "sem_001",
        }
        secret_pass = "ExtremelyConfidentialPassword999!"

        stdout_buf = io.StringIO()
        with patch("tools.login_cli.CRED_FILE", self.cred_path), \
             patch("tools.login_cli.STATE_FILE", self.state_path), \
             patch("getpass.getpass", return_value=secret_pass), \
             patch("core.auth_manager.AuthManager.login", return_value=mock_login_data), \
             patch("core.auth_manager.AuthManager.save_state_atomically"), \
             patch("sys.stdout", stdout_buf), \
             patch("sys.argv", ["login_cli.py"]):
            login_cli.main()

        output = stdout_buf.getvalue()
        self.assertNotIn(secret_pass, output, "密码泄漏在标准输出中！")

    # ==================== A23: DPAPI 加密失败 (不允许明文保存) ====================
    def test_A23_dpapi_failure_rejects_plaintext_fallback(self):
        storage = CredentialStorage(self.cred_path)
        # 预设一个已有的旧凭据
        storage.save_credential("13800138000", "old_plain_sig", "1", use_dpapi=False)

        # 模拟 DPAPI 加密报错
        with patch("core.credential_storage.dpapi_protect", side_effect=OSError("DPAPI CryptProtectData 硬件故障")), \
             patch("core.credential_storage.is_windows", return_value=True):
            with self.assertRaises(CredentialError) as ctx:
                storage.save_credential("13800138000", "new_secret_sig", "1", use_dpapi=True)
            self.assertIn("拒绝保存", str(ctx.exception))

        # 确认原凭据依然完好，且未被改写为新的明文
        current = storage.load_credential()
        self.assertEqual(current["signDigital"], "old_plain_sig")

    # ==================== A24: DPAPI 解密失败 (返回明确异常) ====================
    def test_A24_dpapi_unprotect_failure_raises_credential_error(self):
        # 写入一个损坏的密文字段
        bad_payload = {
            "account": "13800138000",
            "signDigitalEncrypted": base64.b64encode(b"invalid_ciphertext_bytes").decode("ascii"),
            "entrance": "1",
        }
        with open(self.cred_path, "w", encoding="utf-8") as f:
            json.dump(bad_payload, f)

        storage = CredentialStorage(self.cred_path)
        with self.assertRaises(CredentialError) as ctx:
            storage.load_credential()
        self.assertIn("解密", str(ctx.exception))

    # ==================== A25: 账户 A 切换账户 B (身份信息完全隔离) ====================
    def test_A25_account_switch_isolation(self):
        auth_mgr = AuthManager(ssl_verify=False)
        # 账户 A 登录写入
        login_a = {
            "accessToken": "tok_A",
            "refreshToken": "ref_A",
            "schoolId": "sch_A",
            "semesterId": "sem_A",
            "id": "uid_A",
            "realName": "张三",
            "mobile": "13800000001",
            "organizationUserNumber": "20210001",
            "campusId": "cam_A",
        }
        auth_mgr.save_state_atomically(login_a, self.state_path)

        # 账户 B 登录写入
        login_b = {
            "accessToken": "tok_B",
            "refreshToken": "ref_B",
            "schoolId": "sch_B",
            "semesterId": "sem_B",
            "id": "uid_B",
            "realName": "李四",
            "mobile": "13800000002",
            "organizationUserNumber": "20210002",
            "campusId": "cam_B",
        }
        auth_mgr.save_state_atomically(login_b, self.state_path)

        # 验证 state.json 中全部为账户 B 的身份信息，绝无账户 A 残留
        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)

        self.assertEqual(st["userId"], "uid_B")
        self.assertEqual(st["realName"], "李四")
        self.assertEqual(st["mobile"], "13800000002")
        self.assertEqual(st["organizationUserNumber"], "20210002")
        self.assertEqual(st["campusId"], "cam_B")
        self.assertEqual(st["schoolId"], "sch_B")

    # ==================== A26: 新账户字段缺失 (不继承旧账户身份) ====================
    def test_A26_new_account_missing_fields_never_inherits_old_identity(self):
        auth_mgr = AuthManager(ssl_verify=False)
        # 账户 A 登录
        login_a = {
            "accessToken": "tok_A",
            "refreshToken": "ref_A",
            "schoolId": "sch_A",
            "semesterId": "sem_A",
            "id": "uid_A",
            "realName": "张三",
            "mobile": "13800000001",
            "organizationUserNumber": "20210001",
            "campusId": "cam_A",
        }
        auth_mgr.save_state_atomically(login_a, self.state_path)

        # 账户 B 登录，部分身份字段缺失
        login_b = {
            "accessToken": "tok_B",
            "refreshToken": "ref_B",
            "schoolId": "sch_B",
            "semesterId": "sem_B",
            "id": "uid_B",
            # 缺少 realName, mobile, organizationUserNumber, campusId
        }
        auth_mgr.save_state_atomically(login_b, self.state_path)

        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)

        self.assertEqual(st["userId"], "uid_B")
        self.assertEqual(st["realName"], "", "新账户不应继承旧账户的 realName")
        self.assertEqual(st["mobile"], "", "新账户不应继承旧账户的 mobile")
        self.assertEqual(st["organizationUserNumber"], "", "新账户不应继承旧账户的 organizationUserNumber")

    # ==================== A27: state 临时文件写入失败 (原文件不受影响) ====================
    def test_A27_state_tmp_write_failure_preserves_old(self):
        auth_mgr = AuthManager(ssl_verify=False)
        orig_state = {"accessToken": "orig_tok", "refreshToken": "orig_ref", "schoolId": "s", "semesterId": "sem"}
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(orig_state, f)

        login_data = {"accessToken": "new_tok", "refreshToken": "new_ref", "schoolId": "s", "semesterId": "sem"}
        real_open = open

        def faulty_open(path, *args, **kwargs):
            if str(path).endswith(".tmp"):
                raise IOError("模拟磁盘写入故障")
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=faulty_open):
            with self.assertRaises(AuthError):
                auth_mgr.save_state_atomically(login_data, self.state_path)

        with open(self.state_path, "r", encoding="utf-8") as f:
            curr = json.load(f)
        self.assertEqual(curr["accessToken"], "orig_tok")

    # ==================== A28: state 原子替换失败 (原文件不受影响) ====================
    def test_A28_state_replace_failure_preserves_old(self):
        auth_mgr = AuthManager(ssl_verify=False)
        orig_state = {"accessToken": "orig_tok", "refreshToken": "orig_ref", "schoolId": "s", "semesterId": "sem"}
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(orig_state, f)

        login_data = {"accessToken": "new_tok", "refreshToken": "new_ref", "schoolId": "s", "semesterId": "sem"}

        with patch("os.replace", side_effect=OSError("模拟权限拒绝或锁占用")):
            with self.assertRaises(AuthError):
                auth_mgr.save_state_atomically(login_data, self.state_path)

        with open(self.state_path, "r", encoding="utf-8") as f:
            curr = json.load(f)
        self.assertEqual(curr["accessToken"], "orig_tok")

    # ==================== A29: credential 写入失败 (原凭据不受影响) ====================
    def test_A29_credential_write_failure_preserves_old(self):
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "orig_sig", "1", use_dpapi=False)

        with patch("os.replace", side_effect=OSError("模拟替换失败")):
            with self.assertRaises(CredentialError):
                storage.save_credential("13800138000", "new_sig", "1", use_dpapi=False)

        loaded = storage.load_credential()
        self.assertEqual(loaded["signDigital"], "orig_sig")

    # ==================== A30: 学期信息获取异常 (保留可诊断错误) ====================
    def test_A30_semester_error_diagnostics(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", return_value=(500, "Internal Server Error")):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("500", err)
            self.assertNotIn("mock_tok", err, "错误诊断中严禁泄露 Token")

        with patch.object(auth_mgr, "get_json", return_value=(200, {"code": 10002, "message": "学期已结课"})):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("学期已结课", err)

    # ==================== A31: 日志脱敏 (不出现完整敏感值) ====================
    def test_A31_comprehensive_log_masking(self):
        # 边界与各种长度
        self.assertEqual(mask_account(""), "***")
        self.assertEqual(mask_account("123"), "***")
        self.assertEqual(mask_account("1234"), "12**4")
        self.assertEqual(mask_account("13912345678"), "139****5678")

        self.assertEqual(mask_secret(""), "None")
        self.assertEqual(mask_secret("short"), "*** (长度=5)")
        long_secret = "abcdef1234567890xyz"
        masked = mask_secret(long_secret)
        self.assertTrue(masked.startswith("abcd...0xyz"))
        self.assertIn("长度=19", masked)
        self.assertNotIn("12345678", masked)

    # ==================== A32: 两种认证方式兼容 (生成的会话均可被 Client 正常加载) ====================
    def test_A32_client_session_compatibility(self):
        auth_mgr = AuthManager(ssl_verify=False)
        cli_login_data = {
            "accessToken": "tok_cli_32",
            "refreshToken": "ref_cli_32",
            "accessTokenExpires": int(time.time() * 1000) + 7200000,
            "id": "uid_32",
            "schoolId": "sch_32",
            "semesterId": "sem_32",
            "realName": "张三",
        }
        st_cli = auth_mgr.save_state_atomically(cli_login_data, self.state_path)

        app_sync_state = {
            "accessToken": "tok_app_32",
            "refreshToken": "ref_app_32",
            "accessTokenExpires": int(time.time() * 1000) + 7200000,
            "userId": "uid_32",
            "schoolId": "sch_32",
            "semesterId": "sem_32",
            "year": "2026",
            "mobile": "13800000000",
            "realName": "张三",
            "organizationUserNumber": "99999999",
            "campusId": "cam_32",
        }

        # 两个 state 分别传入 Client
        client_cli = lejian.Client(st_cli)
        client_app = lejian.Client(app_sync_state)

        self.assertGreater(client_cli.expires_in(), 7000)
        self.assertGreater(client_app.expires_in(), 7000)
        self.assertEqual(client_cli.s["schoolId"], client_app.s["schoolId"])
        self.assertEqual(client_cli.s["semesterId"], client_app.s["semesterId"])

    # ==================== A33: 测试隔离屏障验证 (真实网络访问被立即拦截) ====================
    def test_A33_offline_socket_guard_blocks_real_network(self):
        import socket
        s = socket.socket()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                s.connect(("cpes.legym.cn", 443))
            self.assertIn("测试隔离屏障触发", str(ctx.exception))
        finally:
            s.close()

    # ==================== A34: state 备份失败中止并保护旧数据 ====================
    def test_A34_state_backup_failure_aborts_and_preserves_old(self):
        auth_mgr = AuthManager(ssl_verify=False)
        old_state = {
            "accessToken": "old_token_123",
            "refreshToken": "old_ref_123",
            "schoolId": "old_sch",
            "semesterId": "old_sem",
            "realName": "旧用户",
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(old_state, f)

        new_login = {
            "accessToken": "new_token_456",
            "refreshToken": "new_ref_456",
            "schoolId": "new_sch",
            "semesterId": "new_sem",
            "realName": "新用户",
        }

        with patch("shutil.copy2", side_effect=IOError("模拟备份写入权限受限")):
            with self.assertRaises(AuthError) as ctx:
                auth_mgr.save_state_atomically(new_login, self.state_path, backup=True)
            self.assertIn("备份原有会话失败", str(ctx.exception))

        # 验证原有文件完好无损，且没有临时文件残留
        with open(self.state_path, "r", encoding="utf-8") as f:
            curr = json.load(f)
        self.assertEqual(curr["accessToken"], "old_token_123")
        self.assertEqual(curr["realName"], "旧用户")
        self.assertFalse(os.path.exists(self.state_path + ".tmp"))

    # ==================== A35: credential 备份失败中止并保护旧数据 ====================
    def test_A35_credential_backup_failure_aborts_and_preserves_old(self):
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "sig_version_1", "1", use_dpapi=False)

        with patch("shutil.copy2", side_effect=IOError("模拟凭据备份失败")):
            with self.assertRaises(CredentialError) as ctx:
                storage.save_credential("13800138000", "sig_version_2", "1", use_dpapi=False)
            self.assertIn("备份原有凭据失败", str(ctx.exception))

        loaded = storage.load_credential()
        self.assertEqual(loaded["signDigital"], "sig_version_1")
        self.assertFalse(os.path.exists(self.cred_path + ".tmp"))

    # ==================== A36: 连续保存保证最新状态与历史备份均完好 ====================
    def test_A36_consecutive_saves_preserves_backup_and_latest(self):
        auth_mgr = AuthManager(ssl_verify=False)
        v1 = {"accessToken": "tok_v1", "refreshToken": "ref_v1", "schoolId": "s", "semesterId": "sem1"}
        v2 = {"accessToken": "tok_v2", "refreshToken": "ref_v2", "schoolId": "s", "semesterId": "sem2"}
        v3 = {"accessToken": "tok_v3", "refreshToken": "ref_v3", "schoolId": "s", "semesterId": "sem3"}

        auth_mgr.save_state_atomically(v1, self.state_path)
        auth_mgr.save_state_atomically(v2, self.state_path)

        with open(self.state_path, "r", encoding="utf-8") as f:
            curr = json.load(f)
        self.assertEqual(curr["accessToken"], "tok_v2")

        bak_path = self.state_path + ".bak"
        self.assertTrue(os.path.exists(bak_path))
        with open(bak_path, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
        self.assertEqual(bak_data["accessToken"], "tok_v1")

        auth_mgr.save_state_atomically(v3, self.state_path)
        with open(self.state_path, "r", encoding="utf-8") as f:
            curr = json.load(f)
        self.assertEqual(curr["accessToken"], "tok_v3")
        with open(bak_path, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
        self.assertEqual(bak_data["accessToken"], "tok_v2")

    # ==================== A37: 学期异常矩阵 - HTTP 401 ====================
    def test_A37_semester_error_matrix_http_401(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", return_value=(401, {"message": "Unauthorized"})):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("401", err)
            self.assertIn("未授权", err)

    # ==================== A38: 学期异常矩阵 - HTTP 403 ====================
    def test_A38_semester_error_matrix_http_403(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", return_value=(403, {"message": "Forbidden"})):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("403", err)
            self.assertIn("禁止访问", err)

    # ==================== A39: 学期异常矩阵 - HTTP 502 ====================
    def test_A39_semester_error_matrix_http_502(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", return_value=(502, "Bad Gateway")):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("502", err)
            self.assertIn("网关错误", err)

    # ==================== A40: 学期异常矩阵 - 非法 JSON ====================
    def test_A40_semester_error_matrix_malformed_json(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", return_value=(200, {"_raw": "<html>500 Error</html>"})):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("不是有效 JSON", err)

    # ==================== A41: 学期异常矩阵 - 响应缺少 data ====================
    def test_A41_semester_error_matrix_missing_data(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", return_value=(200, {"code": 0, "message": "OK", "data": None})):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("缺少 data", err)

    # ==================== A42: 学期异常矩阵 - data 缺少 semesterId ====================
    def test_A42_semester_error_matrix_missing_semester_id(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", return_value=(200, {"code": 0, "message": "OK", "data": {"yearRange": "2026-2027"}})):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("缺少 semesterId", err)

    # ==================== A43: 学期异常矩阵 - 网络超时 ====================
    def test_A43_semester_error_matrix_network_timeout(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", side_effect=AuthNetworkError("请求超时: timed out")):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("网络通信异常", err)
            self.assertIn("超时", err)

    # ==================== A44: 学期异常矩阵 - 网络连接异常 ====================
    def test_A44_semester_error_matrix_connection_error(self):
        auth_mgr = AuthManager(ssl_verify=False)
        with patch.object(auth_mgr, "get_json", side_effect=AuthNetworkError("网络连接异常: [Errno 11001] getaddrinfo failed")):
            sem_id, yr, err = auth_mgr.fetch_semester_info("mock_tok", "sch_001")
            self.assertIsNone(sem_id)
            self.assertIn("网络通信异常", err)
            self.assertIn("网络连接异常", err)

    # ==================== A45: 学期异常矩阵 - 错误信息严禁泄露 Token ====================
    def test_A45_semester_error_matrix_token_desensitized(self):
        auth_mgr = AuthManager(ssl_verify=False)
        secret_token = "ultra_secret_access_token_123456789"
        with patch.object(auth_mgr, "get_json", side_effect=AuthNetworkError(f"请求失败包含敏感令牌: {secret_token}")):
            sem_id, yr, err = auth_mgr.fetch_semester_info(secret_token, "sch_001")
            self.assertIsNone(sem_id)
            self.assertNotIn(secret_token, err)
            self.assertIn("ultr...6789", err)


if __name__ == "__main__":
    unittest.main()
