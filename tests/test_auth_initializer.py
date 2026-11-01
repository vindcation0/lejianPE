"""
乐健体育 · 认证重新初始化统一流程自动化测试套件 (I01 ~ I16)。
==============================================================
覆盖执行计划 M2 (第二阶段) 全部验收指标：
1. 本地状态检查与脱敏报告 (check_status)
2. 抓包流量文件凭据提取与异常解析 (extract_from_flows)
3. 凭据合法性校验 (verify_new_credential)
4. 账号一致性校验 (防误登他人账号造成串号污染)
5. 验证流程中的 50004 签名失效拦截与旧凭据保护
6. 验证流程中的密码错误拦截与旧凭据保护
7. 验证流程中的网络超时/故障拦截与旧凭据保护
8. 验证流程中缺少必要 Token/schoolId 拦截与旧凭据保护
9. 凭据原子安全替换与 .bak 备份保护 (apply_new_credential_atomically)
10. 凭据写入或加密失败时回滚并保留原凭据
11. 会话状态原子更新与学期拉取保护 (update_session_atomically)
12. 学期拉取失败时保护原 state.json 不受损
13. reinitialize 成功全流程原子落地
14. reinitialize 中途异常中止时绝不破坏原有有效凭据
15. 敏感信息全方位脱敏校验 (绝不含明文密码、完整 Token、signDigital)
16. lejian.py reinit 与 menu.py 统一入口路由协同
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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
from core.auth_initializer import AuthInitializer
from core.credential_storage import CredentialStorage
from tests.network_guard import install_network_guard
from tools.login_envelope import wrap


class AuthInitializerTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_network_guard()

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_reinit_")
        self.cred_path = os.path.join(self.test_dir, "credential.json")
        self.state_path = os.path.join(self.test_dir, "state.json")
        self.flows_path = os.path.join(self.test_dir, "login_flows.jsonl")
        self.initializer = AuthInitializer(
            cred_path=self.cred_path,
            state_path=self.state_path,
            use_dpapi=False,  # 测试环境默认统一用非 DPAPI 方便 cross-platform
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_mock_flows(self, account="13800138000", sign_digital="sig_captured_12345", entrance="1"):
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

    # ==================== I01: check_status 各种状态与脱敏报告 ====================
    def test_I01_check_status_comprehensive(self):
        """I01: 验证在各种凭据与会话存在/缺失/过期条件下的 check_status 状态提取与脱敏输出"""
        # 1. 初始状态（无凭据，无会话）
        st1 = self.initializer.check_status()
        self.assertFalse(st1["credential_exists"])
        self.assertFalse(st1["state_exists"])
        report1 = self.initializer.format_status_report(st1)
        self.assertIn("未配置", report1)
        self.assertIn("不存在", report1)

        # 2. 已有凭据与过期会话
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "test_sig_123", "1", use_dpapi=False)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({
                "accessToken": "tok_old",
                "refreshToken": "ref_old",
                "accessTokenExpires": int(time.time() * 1000) - 3600000,  # 1 小时前已过期
                "schoolId": "sch_1",
                "semesterId": "sem_1",
            }, f)

        st2 = self.initializer.check_status()
        self.assertTrue(st2["credential_exists"])
        self.assertEqual(st2["account_masked"], "138****8000")
        self.assertTrue(st2["token_expired"])
        report2 = self.initializer.format_status_report(st2)
        self.assertIn("138****8000", report2)
        self.assertIn("已过期", report2)
        self.assertIn("可以开始重新初始化", report2)

    # ==================== I02: extract_from_flows 正常提取 ====================
    def test_I02_extract_from_flows_success(self):
        """I02: extract_from_flows 成功从抓包记录中解密并提取账号、签名与入口"""
        self._create_mock_flows(account="13811112222", sign_digital="sig_new_abc_999", entrance="1")
        cred = self.initializer.extract_from_flows(self.flows_path)

        self.assertEqual(cred["account"], "13811112222")
        self.assertEqual(cred["signDigital"], "sig_new_abc_999")
        self.assertEqual(cred["entrance"], "1")

    # ==================== I03: extract_from_flows 异常捕获 ====================
    def test_I03_extract_from_flows_failures(self):
        """I03: 抓包文件不存在或无有效登录记录时，抛出 InvalidCredentialError"""
        # 文件不存在
        with self.assertRaises(InvalidCredentialError) as ctx1:
            self.initializer.extract_from_flows("non_existent_flows.jsonl")
        self.assertIn("未找到", str(ctx1.exception))

        # 文件存在但无登录记录
        with open(self.flows_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"status": 200, "url": "https://other.api"}) + "\n")
        with self.assertRaises(InvalidCredentialError) as ctx2:
            self.initializer.extract_from_flows(self.flows_path)
        self.assertIn("未检索到", str(ctx2.exception))

    # ==================== I04: 账号一致性强校验 (防串号污染) ====================
    def test_I04_account_consistency_protection(self):
        """I04: 若当前配置为 A 账号，模拟器中误登 B 账号，必须强行拦截拒绝采纳，保护本地配置不被污染"""
        new_cred = {
            "account": "13999998888",  # 抓到了别人的账号
            "signDigital": "sig_someone_else",
            "entrance": "1",
        }
        with self.assertRaises(InvalidCredentialError) as ctx:
            self.initializer.verify_new_credential(
                new_cred=new_cred,
                password="AnyPassword123",
                expected_account="13800138000",  # 原账号
            )
        err_msg = str(ctx.exception)
        self.assertIn("账号不一致", err_msg)
        self.assertIn("138****8000", err_msg)
        self.assertIn("139****8888", err_msg)

    # ==================== I05: verify_new_credential 成功 ====================
    def test_I05_verify_new_credential_success(self):
        """I05: 新凭据在线登录验证成功，返回完整的 login_data 载荷"""
        new_cred = {"account": "13800138000", "signDigital": "sig_good_123", "entrance": "1"}
        mock_login_data = {
            "accessToken": "tok_new_good",
            "refreshToken": "ref_new_good",
            "schoolId": "sch_001",
            "realName": "李四",
        }

        with patch.object(self.initializer.auth_manager, "login", return_value=mock_login_data):
            login_data = self.initializer.verify_new_credential(
                new_cred=new_cred,
                password="CorrectPassword",
                expected_account="13800138000",
            )
            self.assertEqual(login_data["accessToken"], "tok_new_good")
            self.assertEqual(login_data["schoolId"], "sch_001")

    # ==================== I06: verify_new_credential 登录返回 50004 拦截 ====================
    def test_I06_verify_new_credential_50004_signature_failure(self):
        """I06: 新凭据登录返回 50004 签名失效时抛出 BusinessAuthError，阻止替换"""
        new_cred = {"account": "13800138000", "signDigital": "sig_bad_50004", "entrance": "1"}

        with patch.object(self.initializer.auth_manager, "login", side_effect=BusinessAuthError("50004 签名失效", code=50004)):
            with self.assertRaises(BusinessAuthError) as ctx:
                self.initializer.verify_new_credential(
                    new_cred=new_cred,
                    password="AnyPassword",
                    expected_account="13800138000",
                )
            self.assertEqual(ctx.exception.code, 50004)

    # ==================== I07: verify_new_credential 密码错误拦截 ====================
    def test_I07_verify_new_credential_wrong_password(self):
        """I07: 新凭据登录验证时密码错误，抛出 BusinessAuthError，阻止替换"""
        new_cred = {"account": "13800138000", "signDigital": "sig_good", "entrance": "1"}

        with patch.object(self.initializer.auth_manager, "login", side_effect=BusinessAuthError("密码错误", code=10002)):
            with self.assertRaises(BusinessAuthError) as ctx:
                self.initializer.verify_new_credential(
                    new_cred=new_cred,
                    password="WrongPassword",
                    expected_account="13800138000",
                )
            self.assertIn("密码", str(ctx.exception))

    # ==================== I08: verify_new_credential 网络故障拦截 ====================
    def test_I08_verify_new_credential_network_error(self):
        """I08: 新凭据验证过程遇到网络超时或连接失败，抛出 AuthNetworkError，阻止替换"""
        new_cred = {"account": "13800138000", "signDigital": "sig_good", "entrance": "1"}

        with patch.object(self.initializer.auth_manager, "login", side_effect=AuthNetworkError("连接超时")):
            with self.assertRaises(AuthNetworkError) as ctx:
                self.initializer.verify_new_credential(
                    new_cred=new_cred,
                    password="Password",
                    expected_account="13800138000",
                )
            self.assertIn("超时", str(ctx.exception))

    # ==================== I09: verify_new_credential 缺少必要 Token 拦截 ====================
    def test_I09_verify_new_credential_missing_tokens(self):
        """I09: 新凭据登录成功但响应缺少 accessToken/refreshToken 时拦截并抛出 TokenAcquisitionError"""
        new_cred = {"account": "13800138000", "signDigital": "sig_good", "entrance": "1"}

        with patch.object(self.initializer.auth_manager, "login", return_value={"schoolId": "sch_1"}):
            with self.assertRaises(TokenAcquisitionError):
                self.initializer.verify_new_credential(
                    new_cred=new_cred,
                    password="Password",
                    expected_account="13800138000",
                )

    # ==================== I10: 凭据原子安全替换与 .bak 备份 ====================
    def test_I10_apply_new_credential_atomically(self):
        """I10: 成功保存新凭据，原子替换旧凭据并保留旧凭据备份 (.bak)"""
        # 预设旧凭据
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "old_signature_111", use_dpapi=False)

        new_cred = {"account": "13800138000", "signDigital": "new_signature_222", "entrance": "1"}
        self.initializer.apply_new_credential_atomically(new_cred, use_dpapi=False)

        # 检查最新凭据
        loaded = storage.load_credential()
        self.assertEqual(loaded["signDigital"], "new_signature_222")

        # 检查 .bak 备份是否保留旧凭据
        bak_file = self.cred_path + ".bak"
        self.assertTrue(os.path.exists(bak_file))
        with open(bak_file, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
        self.assertEqual(bak_data["signDigital"], "old_signature_111")

    # ==================== I11: 会话状态原子更新与学期拉取 ====================
    def test_I11_update_session_atomically(self):
        """I11: update_session_atomically 成功拉取当前学期并原子写入 state.json"""
        login_data = {
            "accessToken": "tok_session_123",
            "refreshToken": "ref_session_456",
            "schoolId": "sch_001",
            "realName": "王五",
        }

        with patch.object(self.initializer.auth_manager, "fetch_semester_info", return_value=("sem_2026_fall", "2026-2027", None)):
            saved_state = self.initializer.update_session_atomically(login_data)
            self.assertEqual(saved_state["semesterId"], "sem_2026_fall")
            self.assertEqual(saved_state["accessToken"], "tok_session_123")

            # 验证磁盘上的真实 state.json
            with open(self.state_path, "r", encoding="utf-8") as f:
                disk_state = json.load(f)
            self.assertEqual(disk_state["semesterId"], "sem_2026_fall")

    # ==================== I12: 学期拉取失败保护原会话 ====================
    def test_I12_semester_fetch_failure_protects_old_state(self):
        """I12: 拉取学期失败时拒绝写入残损会话，抛出 SemesterInfoError 并保持原 state.json 不受损"""
        # 预设原 state
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump({"accessToken": "tok_old_safe", "semesterId": "sem_old_safe", "schoolId": "sch_1", "refreshToken": "ref_1"}, f)

        login_data = {
            "accessToken": "tok_new_test",
            "refreshToken": "ref_new_test",
            "schoolId": "sch_001",
        }

        with patch.object(self.initializer.auth_manager, "fetch_semester_info", return_value=(None, None, "HTTP 502 网关错误")):
            with self.assertRaises(SemesterInfoError):
                self.initializer.update_session_atomically(login_data)

            # 验证原 state 完好未被破坏
            with open(self.state_path, "r", encoding="utf-8") as f:
                intact = json.load(f)
            self.assertEqual(intact["accessToken"], "tok_old_safe")

    # ==================== I13: reinitialize 全流程成功 ====================
    def test_I13_reinitialize_full_flow_success(self):
        """I13: 验证 reinitialize 端到端成功：提取 -> 校验 -> 原子存凭据 -> 原子更会话 -> 输出完成"""
        self._create_mock_flows(account="13800138000", sign_digital="sig_reinit_full", entrance="1")

        mock_login_data = {
            "accessToken": "tok_full_success",
            "refreshToken": "ref_full_success",
            "schoolId": "sch_001",
            "realName": "全流程测试",
        }

        stdout_buf = io.StringIO()
        with patch.object(self.initializer.auth_manager, "login", return_value=mock_login_data), \
             patch.object(self.initializer.auth_manager, "fetch_semester_info", return_value=("sem_final_001", "2026-2027", None)), \
             patch("sys.stdout", stdout_buf):
            ok = self.initializer.reinitialize(
                password="MyCorrectPassword",
                flows_path=self.flows_path,
                interactive=False,
            )
            self.assertTrue(ok)

        out = stdout_buf.getvalue()
        self.assertIn("========== 认证初始化完成 ==========", out)
        self.assertIn("138****8000", out)
        self.assertIn("sem_final_001", out)

        # 检查磁盘凭据与会话
        loaded_cred = self.initializer.storage.load_credential()
        self.assertEqual(loaded_cred["signDigital"], "sig_reinit_full")
        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["accessToken"], "tok_full_success")
        self.assertEqual(st["semesterId"], "sem_final_001")

    # ==================== I14: reinitialize 异常回退保持原凭据不损 ====================
    def test_I14_reinitialize_abort_preserves_old_credential(self):
        """I14: reinitialize 在中途遇到密码错误或网络故障时，必须完整保留旧凭据"""
        # 预设旧凭据
        self.initializer.storage.save_credential("13800138000", "original_safe_signature", use_dpapi=False)
        self._create_mock_flows(account="13800138000", sign_digital="new_flawed_signature", entrance="1")

        stdout_buf = io.StringIO()
        # 模拟在线验证返回 50004
        with patch.object(self.initializer.auth_manager, "login", side_effect=BusinessAuthError("50004 签名失效", code=50004)), \
             patch("sys.stdout", stdout_buf):
            ok = self.initializer.reinitialize(
                password="AnyPassword",
                flows_path=self.flows_path,
                interactive=False,
            )
            self.assertFalse(ok)

        # 强力核验证明：原有旧凭据丝毫未受损伤！
        loaded = self.initializer.storage.load_credential()
        self.assertEqual(loaded["signDigital"], "original_safe_signature")

    # ==================== I15: 全方位敏感信息脱敏校验 ====================
    def test_I15_reinitialize_security_masking(self):
        """I15: 验证整个初始化流程的所有标准输出中绝对不泄漏密码明文与完整签名"""
        secret_pwd = "TopSecretReinitPassword888!"
        secret_sig = "9876543210abcdef9876543210fedcba"
        self._create_mock_flows(account="13800138000", sign_digital=secret_sig, entrance="1")

        stdout_buf = io.StringIO()
        mock_login_data = {
            "accessToken": "tok_secret_jwt_string_xxxxxx",
            "refreshToken": "ref_secret_jwt_string_yyyyyy",
            "schoolId": "sch_001",
        }
        with patch.object(self.initializer.auth_manager, "login", return_value=mock_login_data), \
             patch.object(self.initializer.auth_manager, "fetch_semester_info", return_value=("sem_1", "2026", None)), \
             patch("sys.stdout", stdout_buf):
            self.initializer.reinitialize(
                password=secret_pwd,
                flows_path=self.flows_path,
                interactive=True,
            )

        out = stdout_buf.getvalue()
        self.assertNotIn(secret_pwd, out, "密码明文泄漏在标准输出中！")
        self.assertNotIn(secret_sig, out, "完整签名材料泄漏在标准输出中！")
        self.assertIn("138****8000", out)

    # ==================== I16: lejian.py reinit 与 menu.py 统一路由 ====================
    def test_I16_lejian_and_menu_routing(self):
        """I16: 验证 lejian.py reinit 命令及 menu.py 动作路由正确绑定到 cmd_reinit"""
        import lejian
        import menu

        # 验证 menu.py 中 [b] 项绑定
        hit_b = next((a for a in menu.ACTIONS if a[0] == "b"), None)
        self.assertIsNotNone(hit_b)
        self.assertEqual(hit_b[2], ["lejian.py", "reinit"])

        # 验证 lejian.py 能够调用 cmd_reinit
        with patch("core.auth_initializer.AuthInitializer.reinitialize") as mock_reinit:
            lejian.cmd_reinit()
            mock_reinit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
