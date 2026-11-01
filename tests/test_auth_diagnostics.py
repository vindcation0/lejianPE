"""
乐健体育 · 登录失败自动诊断模块自动化测试套件 (D01 ~ D16)。
==============================================================
覆盖执行计划 M1 (第三阶段) 全部验收指标：
1. 50004 签名错误识别与客观原因分析 (不武断下定论)
2. 明确密码/账号错误分类与提示
3. HTTP 401 / Token 过期分类与提示
4. HTTP 429 请求过于频繁限流保护分类与提示
5. HTTP 500 / 502 / 504 服务端故障分类与提示
6. 网络连接超时 (TimeoutError) 分类与提示
7. DNS 解析失败 (URLError / getaddrinfo) 分类与提示
8. 登录响应 AES 信封解密失败分类与保护
9. 登录响应缺少关键字段 (缺少 Token / schoolId) 分类
10. credential.json 损坏或格式错误分类
11. Windows DPAPI 平台限制或解密失败分类
12. 未知业务错误码安全兜底
13. 敏感信息全方位脱敏校验 (绝不含明文密码、完整 Token、signDigital)
14. 诊断过程无额外网络副作用 (不重复发送网络请求)
15. 交互式引导与非交互式静默模式完整覆盖
16. tools/login_cli.py 集成诊断无缝协同
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
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.auth_diagnostics import (
    ErrorCategory,
    AuthDiagnosticReport,
    diagnose_auth_error,
    handle_login_failure,
    mask_sensitive_text,
)
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
from core.credential_storage import CredentialStorage
from tests.network_guard import install_network_guard


class AuthDiagnosticsTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        install_network_guard()

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_diag_")
        self.cred_path = os.path.join(self.test_dir, "credential.json")
        self.state_path = os.path.join(self.test_dir, "state.json")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # ==================== D01: 50004 签名错误 ====================
    def test_D01_signature_error_50004(self):
        """D01: 服务端返回 50004 准确归类为 SIGNATURE_ERROR，给出多种可能原因与重新初始化建议"""
        exc = BusinessAuthError("认证失败 (code=50004): 签名材料失效或密码已被修改", code=50004)
        report = diagnose_auth_error(exc, context={"account": "13800138000"})

        self.assertEqual(report.error_category, ErrorCategory.SIGNATURE_ERROR)
        self.assertEqual(report.business_code, 50004)
        self.assertTrue(report.can_reinit)
        self.assertFalse(report.can_retry)
        self.assertIn("138****8000", report.account_masked)

        # 验证原因不武断断定是改密码，包含密码修改、签名失效等多种客观原因
        causes_text = " ".join(report.possible_causes)
        self.assertIn("密码", causes_text)
        self.assertIn("signDigital", causes_text)

        report_str = report.format_report()
        self.assertIn("========== 登录诊断 ==========", report_str)
        self.assertIn("认证签名错误 (50004)", report_str)

    # ==================== D02: 明确密码错误 ====================
    def test_D02_wrong_password_business_failure(self):
        """D02: 服务端明确返回密码错误准确归类为 WRONG_PASSWORD，引导核对输入而非误导重抓签名"""
        exc = BusinessAuthError("认证失败 (code=10002): 账号或密码错误 (密码不正确)", code=10002)
        report = diagnose_auth_error(exc, context={"account": "13912345678"})

        self.assertEqual(report.error_category, ErrorCategory.WRONG_PASSWORD)
        self.assertFalse(report.can_reinit)
        self.assertTrue(report.can_retry)
        self.assertIn("139****5678", report.account_masked)
        self.assertTrue(any("核对" in act or "密码" in act for act in report.suggested_actions))

    # ==================== D03: HTTP 401 / Token 过期 ====================
    def test_D03_http_401_token_expired(self):
        """D03: HTTP 401 或 Token 过期准确归类为 TOKEN_EXPIRED"""
        exc = AuthNetworkError("HTTP 401 未授权（Token 无效或已过期）", http_status=401)
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.TOKEN_EXPIRED)
        self.assertEqual(report.http_status, 401)
        self.assertTrue(report.can_retry)

    # ==================== D04: HTTP 429 限流保护 ====================
    def test_D04_http_429_rate_limiting(self):
        """D04: HTTP 429 请求过于频繁归类为 CLIENT_PARAM_ERROR 并提示稍候重试"""
        exc = AuthNetworkError("服务端返回 HTTP 429: Too Many Requests", http_status=429)
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.CLIENT_PARAM_ERROR)
        self.assertEqual(report.http_status, 429)
        self.assertIn("频繁", report.summary)
        self.assertTrue(any("等待" in act for act in report.suggested_actions))

    # ==================== D05: HTTP 500 / 502 服务端故障 ====================
    def test_D05_http_5xx_server_error(self):
        """D05: HTTP 500 / 502 等基础设施故障归类为 SERVER_ERROR，提示服务端维护与稍后重试"""
        for code in (500, 502, 503, 504):
            exc = AuthNetworkError(f"HTTP {code} 基础设施故障", http_status=code)
            report = diagnose_auth_error(exc)
            self.assertEqual(report.error_category, ErrorCategory.SERVER_ERROR)
            self.assertEqual(report.http_status, code)
            self.assertTrue(any("稍等" in act or "维护" in c for act in report.suggested_actions for c in report.possible_causes))

    # ==================== D06: 连接超时 ====================
    def test_D06_connection_timeout(self):
        """D06: 连接超时异常准确归类为 NETWORK_ERROR，提示检查网络与代理"""
        exc = AuthNetworkError("请求超时: The read operation timed out")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.NETWORK_ERROR)
        self.assertTrue(any("代理" in act or "网络" in act for act in report.suggested_actions))

    # ==================== D07: DNS 解析失败 ====================
    def test_D07_dns_resolution_failure(self):
        """D07: DNS 域名解析失败准确归类为 NETWORK_ERROR"""
        exc = urllib.error.URLError("getaddrinfo failed: [Errno 11001] getaddrinfo failed")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.NETWORK_ERROR)
        self.assertTrue(any("DNS" in c for c in report.possible_causes))

    # ==================== D08: 响应解密失败 ====================
    def test_D08_envelope_decrypt_failure(self):
        """D08: AES 信封载荷 (pyd) 解密失败归类为 DECRYPT_FAILED 并提示检查时间与格式"""
        exc = TokenAcquisitionError("解密服务端返回的登录载荷失败: Padding is incorrect.")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.DECRYPT_FAILED)
        self.assertTrue(any("时间" in act or "协议" in c for act in report.suggested_actions for c in report.possible_causes))

    # ==================== D09: 响应缺少 Token ====================
    def test_D09_missing_required_token_field(self):
        """D09: 响应缺少 Token 或 schoolId 归类为 MALFORMED_RESPONSE，指出停止落地残缺会话"""
        exc = TokenAcquisitionError("登录成功但未能获取到 accessToken")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.MALFORMED_RESPONSE)
        self.assertTrue(any("安全屏障" in act or "未被修改" in act for act in report.suggested_actions))

    # ==================== D10: credential.json 损坏或缺失 ====================
    def test_D10_credential_corrupted_or_missing(self):
        """D10: 本地凭据损坏或缺少必要字段归类为 CREDENTIAL_CORRUPTED，引导重新初始化"""
        exc1 = CredentialError("未找到凭据文件: credential.json")
        report1 = diagnose_auth_error(exc1)
        self.assertEqual(report1.error_category, ErrorCategory.CREDENTIAL_CORRUPTED)
        self.assertTrue(report1.can_reinit)

        exc2 = InvalidCredentialError("凭据缺少认证签名 (signDigital)")
        report2 = diagnose_auth_error(exc2)
        self.assertEqual(report2.error_category, ErrorCategory.CREDENTIAL_CORRUPTED)
        self.assertTrue(report2.can_reinit)

    # ==================== D11: Windows DPAPI 平台限制与解密失败 ====================
    def test_D11_dpapi_unavailable_or_failure(self):
        """D11: Windows DPAPI 解密失败或跨平台限制归类为 DPAPI_UNAVAILABLE"""
        exc = CredentialError("解密凭据 signDigital 失败: DPAPI CryptUnprotectData 解密失败")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.DPAPI_UNAVAILABLE)
        self.assertTrue(report.can_reinit)
        self.assertTrue(any("DPAPI" in c or "用户" in c for c in report.possible_causes))

    # ==================== D12: 未知业务错误码安全兜底 ====================
    def test_D12_unknown_business_error_code(self):
        """D12: 无法识别的未知业务错误码安全兜底为 UNKNOWN_ERROR，明确说明原因尚未确定"""
        exc = BusinessAuthError("认证失败 (HTTP 200, code=99999): 未知的非标服务端异常", code=99999)
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.UNKNOWN_ERROR)
        self.assertEqual(report.business_code, 99999)
        self.assertTrue(any("尚未" in c for c in report.possible_causes))

    # ==================== D13: 敏感信息全方位脱敏校验 ====================
    def test_D13_security_masking_comprehensive(self):
        """D13: 验证所有诊断输出中绝对不泄漏密码明文、完整 Token、signDigital"""
        raw_evil_text = (
            "POST https://cpes.legym.cn/authorization/user/v2/manage/login "
            "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.evilpayload.signature "
            "signDigital: 74a9840283f5091741639d4b68e9d0fa734b "
            "password: SuperSecretPassword123! "
            "account: 13800138000"
        )
        masked = mask_sensitive_text(raw_evil_text)

        self.assertNotIn("SuperSecretPassword123!", masked)
        self.assertNotIn("74a9840283f5091741639d4b68e9d0fa734b", masked)
        self.assertNotIn("evilpayload", masked)

        # 检查诊断报告生成的文本
        exc = BusinessAuthError(raw_evil_text, code=50004)
        report = diagnose_auth_error(exc, context={"account": "13800138000"})
        report_text = report.format_report(verbose=True)

        self.assertNotIn("SuperSecretPassword123!", report_text)
        self.assertNotIn("74a9840283f5091741639d4b68e9d0fa734b", report_text)
        self.assertNotIn("evilpayload", report_text)
        self.assertIn("138****8000", report_text)

    # ==================== D14: 诊断过程无额外网络请求 ====================
    def test_D14_diagnostic_causes_no_network_requests(self):
        """D14: 验证调用诊断函数不会触发任何网络请求（纯只读离线分析）"""
        exc = BusinessAuthError("50004 签名错误", code=50004)
        with patch("urllib.request.urlopen") as mock_urlopen:
            report = diagnose_auth_error(exc)
            self.assertIsNotNone(report)
            mock_urlopen.assert_not_called()

    # ==================== D15: 交互与非交互模式处理 ====================
    def test_D15_handle_login_failure_modes(self):
        """D15: 验证 handle_login_failure 在非交互式终端下静默输出并在交互式下响应选项"""
        exc = BusinessAuthError("50004 签名错误", code=50004)

        # 1. 非交互式测试
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            rep = handle_login_failure(exc, interactive=False)
            self.assertEqual(rep.error_category, ErrorCategory.SIGNATURE_ERROR)
        self.assertIn("50004", stdout_buf.getvalue())

        # 2. 交互式选择 [2] (退出)
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("builtins.input", return_value="2"):
            rep = handle_login_failure(exc, interactive=True)
            self.assertEqual(rep.error_category, ErrorCategory.SIGNATURE_ERROR)

        # 3. 交互式选择 [1] (触发重初始化回调)
        mock_reinit = MagicMock()
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("builtins.input", return_value="1"):
            handle_login_failure(exc, interactive=True, on_reinit=mock_reinit)
            mock_reinit.assert_called_once()

    # ==================== D16: tools/login_cli.py 集成诊断无缝协同 ====================
    def test_D16_login_cli_integration_with_diagnostics(self):
        """D16: 当 login_cli 发生异常时，统一触发自动诊断报告输出"""
        import tools.login_cli as login_cli
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "dummy_sig_123", "1", use_dpapi=False)

        stdout_buf = io.StringIO()
        # 模拟登录抛出 50004
        with patch("tools.login_cli.CRED_FILE", self.cred_path), \
             patch("tools.login_cli.STATE_FILE", self.state_path), \
             patch("getpass.getpass", return_value="AnyPassword"), \
             patch("core.auth_manager.AuthManager.login", side_effect=BusinessAuthError("50004 签名失效", code=50004)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("sys.stdout", stdout_buf), \
             patch("sys.argv", ["login_cli.py"]):
            with self.assertRaises(SystemExit) as ctx:
                login_cli.main()
            self.assertEqual(ctx.exception.code, 1)

        output = stdout_buf.getvalue()
        self.assertIn("========== 登录诊断 ==========", output)
        self.assertIn("50004", output)
        self.assertIn("138****8000", output)


if __name__ == "__main__":
    unittest.main()
