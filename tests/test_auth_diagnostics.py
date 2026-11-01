"""
乐健体育 · 登录失败自动诊断模块自动化测试套件 (D01 ~ D20)。
==============================================================
覆盖执行计划 M1 及其优化小修正验收指标：
1. 50004 签名错误识别与客观原因分析 (can_reinit=True)
2. 明确密码/账号错误分类与提示 (can_reinit=False, can_retry=True)
3. 细分 HTTP 401：
   - 登录接口 401 -> UNAUTHORIZED (登录凭据无效或未授权, can_reinit=False)
   - 业务接口 401 -> TOKEN_EXPIRED (访问令牌过期, can_reinit=False, can_retry=True)
4. 独立 HTTP 429 (RATE_LIMITED)：
   - 服务端返回 Retry-After 时准确提取与提示冷却秒数
   - 未返回时采用保守等待策略 (60~180s)
   - can_retry=False, can_reinit=False，严禁连续重试
5. HTTP 500 / 502 / 504 服务端故障分类与提示 (can_reinit=False, can_retry=True)
6. 网络连接超时 (TimeoutError) 分类与提示 (can_reinit=False, can_retry=True)
7. DNS 解析失败 (URLError / getaddrinfo) 分类与提示 (can_reinit=False, can_retry=True)
8. 登录响应 AES 信封解密失败分类与保护 (can_reinit=False, can_retry=True)
9. 登录响应缺少关键字段 (缺少 Token / schoolId) 分类 (can_reinit=False, can_retry=False)
10. credential.json 损坏或格式错误分类 (can_reinit=True)
11. Windows DPAPI 凭据安全体系细分：
   - 平台不支持 DPAPI (DPAPI_UNAVAILABLE, Fail-closed, 坚决不降级明文, can_reinit=False)
   - Windows DPAPI 解密失败 (DPAPI_DECRYPT_FAILED, 权限/密文损坏, can_reinit=True)
12. 未知业务错误码安全兜底 (UNKNOWN_ERROR, can_reinit=False 严格不默认重初始化，先诊断！)
13. can_reinit 推荐矩阵严格符合安全策略
14. 敏感信息全方位脱敏校验 (绝不含明文密码、完整 Token、signDigital)
15. 诊断过程无额外网络副作用 (不重复发送网络请求)
16. 交互式引导与非交互式静默模式完整覆盖
17. tools/login_cli.py 集成诊断无缝协同
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
        """D01: 服务端返回 50004 准确归类为 SIGNATURE_ERROR，给出多种可能原因并推荐重新初始化"""
        exc = BusinessAuthError("认证失败 (code=50004): 签名材料失效或密码已被修改", code=50004)
        report = diagnose_auth_error(exc, context={"account": "13800138000"})

        self.assertEqual(report.error_category, ErrorCategory.SIGNATURE_ERROR)
        self.assertEqual(report.business_code, 50004)
        self.assertTrue(report.can_reinit)
        self.assertFalse(report.can_retry)
        self.assertIn("138****8000", report.account_masked)

        causes_text = " ".join(report.possible_causes)
        self.assertIn("密码", causes_text)
        self.assertIn("signDigital", causes_text)

        report_str = report.format_report()
        self.assertIn("========== 登录诊断 ==========", report_str)
        self.assertIn("认证签名错误 (50004)", report_str)

    # ==================== D02: 明确密码错误 ====================
    def test_D02_wrong_password_business_failure(self):
        """D02: 服务端明确返回密码错误归类为 WRONG_PASSWORD，引导核对输入，严格 can_reinit=False"""
        exc = BusinessAuthError("认证失败 (code=10002): 账号或密码错误 (密码不正确)", code=10002)
        report = diagnose_auth_error(exc, context={"account": "13912345678"})

        self.assertEqual(report.error_category, ErrorCategory.WRONG_PASSWORD)
        self.assertFalse(report.can_reinit)
        self.assertTrue(report.can_retry)
        self.assertIn("139****5678", report.account_masked)
        self.assertTrue(any("核对" in act or "密码" in act for act in report.suggested_actions))

    # ==================== D03A: 登录接口 HTTP 401 (UNAUTHORIZED) ====================
    def test_D03A_login_endpoint_http_401_unauthorized(self):
        """D03A: 登录接口返回 HTTP 401 归类为 UNAUTHORIZED (登录凭据无效或未授权)，不直接断定为 Token 过期"""
        exc = AuthNetworkError("登录接口返回 HTTP 401 未授权", http_status=401)
        report = diagnose_auth_error(exc, context={"stage": "login", "path": "authorization/user/v2/manage/login"})

        self.assertEqual(report.error_category, ErrorCategory.UNAUTHORIZED)
        self.assertEqual(report.http_status, 401)
        self.assertFalse(report.can_reinit)
        self.assertFalse(report.can_retry)
        self.assertIn("登录凭据", report.summary)
        self.assertTrue(any("App" in act or "激活" in c for act in report.suggested_actions for c in report.possible_causes))

    # ==================== D03B: 业务接口 HTTP 401 (TOKEN_EXPIRED) ====================
    def test_D03B_business_endpoint_http_401_token_expired(self):
        """D03B: 业务接口返回 HTTP 401 归类为 TOKEN_EXPIRED (访问令牌过期或作废)"""
        exc = AuthNetworkError("HTTP 401 未授权（Token 无效或已过期）", http_status=401)
        report = diagnose_auth_error(exc, context={"stage": "semester", "path": "education/semester/getCurrent"})

        self.assertEqual(report.error_category, ErrorCategory.TOKEN_EXPIRED)
        self.assertEqual(report.http_status, 401)
        self.assertFalse(report.can_reinit)
        self.assertTrue(report.can_retry)
        self.assertIn("访问令牌", report.summary)

    # ==================== D04A: HTTP 429 限流保护 (带 Retry-After) ====================
    def test_D04A_http_429_with_retry_after(self):
        """D04A: HTTP 429 独立分类为 RATE_LIMITED，准确提取 Retry-After 并提示指定冷却秒数"""
        exc = AuthNetworkError("服务端返回 HTTP 429: Too Many Requests. Retry-After: 120", http_status=429)
        report = diagnose_auth_error(exc, context={"retry_after": 120})

        self.assertEqual(report.error_category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(report.http_status, 429)
        self.assertEqual(report.retry_after, 120)
        self.assertFalse(report.can_reinit)
        self.assertFalse(report.can_retry)  # 严禁自动重试
        self.assertIn("120", report.summary)
        self.assertTrue(any("120" in act for act in report.suggested_actions))
        self.assertTrue(any("连续重试" in act for act in report.suggested_actions))

    # ==================== D04B: HTTP 429 限流保护 (无 Retry-After 保守等待) ====================
    def test_D04B_http_429_without_retry_after(self):
        """D04B: HTTP 429 未提供 Retry-After 时采用保守等待策略 (60~180s)，严禁连续重试"""
        exc = AuthNetworkError("服务端返回 HTTP 429: Too Many Requests", http_status=429)
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.RATE_LIMITED)
        self.assertEqual(report.http_status, 429)
        self.assertIsNone(report.retry_after)
        self.assertFalse(report.can_reinit)
        self.assertFalse(report.can_retry)
        self.assertTrue(any("60~180" in act for act in report.suggested_actions))

    # ==================== D05: HTTP 500 / 502 服务端故障 ====================
    def test_D05_http_5xx_server_error(self):
        """D05: HTTP 500 / 502 等基础设施故障归类为 SERVER_ERROR，can_reinit=False"""
        for code in (500, 502, 503, 504):
            exc = AuthNetworkError(f"HTTP {code} 基础设施故障", http_status=code)
            report = diagnose_auth_error(exc)
            self.assertEqual(report.error_category, ErrorCategory.SERVER_ERROR)
            self.assertEqual(report.http_status, code)
            self.assertFalse(report.can_reinit)
            self.assertTrue(report.can_retry)

    # ==================== D06: 连接超时 ====================
    def test_D06_connection_timeout(self):
        """D06: 连接超时异常归类为 NETWORK_ERROR，can_reinit=False"""
        exc = AuthNetworkError("请求超时: The read operation timed out")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.NETWORK_ERROR)
        self.assertFalse(report.can_reinit)
        self.assertTrue(report.can_retry)

    # ==================== D07: DNS 解析失败 ====================
    def test_D07_dns_resolution_failure(self):
        """D07: DNS 域名解析失败归类为 NETWORK_ERROR，can_reinit=False"""
        exc = urllib.error.URLError("getaddrinfo failed: [Errno 11001] getaddrinfo failed")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.NETWORK_ERROR)
        self.assertFalse(report.can_reinit)
        self.assertTrue(any("DNS" in c for c in report.possible_causes))

    # ==================== D08: 响应解密失败 ====================
    def test_D08_envelope_decrypt_failure(self):
        """D08: AES 信封载荷 (pyd) 解密失败归类为 DECRYPT_FAILED，can_reinit=False"""
        exc = TokenAcquisitionError("解密服务端返回的登录载荷失败: Padding is incorrect.")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.DECRYPT_FAILED)
        self.assertFalse(report.can_reinit)

    # ==================== D09: 响应缺少 Token ====================
    def test_D09_missing_required_token_field(self):
        """D09: 响应缺少 Token 归类为 MALFORMED_RESPONSE，can_reinit=False"""
        exc = TokenAcquisitionError("登录成功但未能获取到 accessToken")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.MALFORMED_RESPONSE)
        self.assertFalse(report.can_reinit)

    # ==================== D10: credential.json 损坏或缺失 ====================
    def test_D10_credential_corrupted_or_missing(self):
        """D10: 本地凭据损坏或缺少必要字段归类为 CREDENTIAL_CORRUPTED，推荐重新初始化 (can_reinit=True)"""
        exc1 = CredentialError("未找到凭据文件: credential.json")
        report1 = diagnose_auth_error(exc1)
        self.assertEqual(report1.error_category, ErrorCategory.CREDENTIAL_CORRUPTED)
        self.assertTrue(report1.can_reinit)

        exc2 = InvalidCredentialError("凭据缺少认证签名 (signDigital)")
        report2 = diagnose_auth_error(exc2)
        self.assertEqual(report2.error_category, ErrorCategory.CREDENTIAL_CORRUPTED)
        self.assertTrue(report2.can_reinit)

    # ==================== D11A: 平台不支持 DPAPI (坚决不降级明文) ====================
    def test_D11A_dpapi_unavailable_platform_limit(self):
        """D11A: 非 Windows 环境导致 DPAPI 不可用时归类为 DPAPI_UNAVAILABLE，坚决拒绝明文降级提示，can_reinit=False"""
        exc = CredentialError("当前系统非 Windows，无法使用 DPAPI 安全加密存储")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.DPAPI_UNAVAILABLE)
        self.assertFalse(report.can_reinit)  # 非 Windows 下重抓也无法调用 DPAPI
        report_text = report.format_report()
        self.assertNotIn("切明文模式", report_text)
        self.assertNotIn("使用明文模式", report_text)
        self.assertIn("严禁", report_text)
        self.assertTrue(any("Fail-closed" in c or "跨平台" in act for c in report.possible_causes for act in report.suggested_actions))

    # ==================== D11B: Windows DPAPI 解密失败 (权限/密文损坏) ====================
    def test_D11B_dpapi_decrypt_failure_user_mismatch(self):
        """D11B: Windows 下由于用户不匹配或密文损坏导致解密失败归类为 DPAPI_DECRYPT_FAILED，can_reinit=True"""
        exc = CredentialError("解密凭据 signDigital 失败: DPAPI CryptUnprotectData 解密失败（当前用户无权访问或数据损坏）")
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.DPAPI_DECRYPT_FAILED)
        self.assertTrue(report.can_reinit)  # 允许在当前用户下重新初始化生成专属凭据
        self.assertTrue(any("用户" in c or "Fail-closed" in act for c in report.possible_causes for act in report.suggested_actions))

    # ==================== D12: 未知错误安全兜底 (严格 can_reinit=False) ====================
    def test_D12_unknown_business_error_code(self):
        """D12: 无法识别的未知业务错误码安全兜底为 UNKNOWN_ERROR，严格 can_reinit=False（先诊断，不盲目重抓）"""
        exc = BusinessAuthError("认证失败 (HTTP 200, code=99999): 未知的非标服务端异常", code=99999)
        report = diagnose_auth_error(exc)

        self.assertEqual(report.error_category, ErrorCategory.UNKNOWN_ERROR)
        self.assertEqual(report.business_code, 99999)
        self.assertFalse(report.can_reinit)  # 严格按用户决策设定为 False！
        self.assertFalse(report.can_retry)
        self.assertTrue(any("尚未" in c for c in report.possible_causes))
        self.assertTrue(any("避免盲目" in act for act in report.suggested_actions))

    # ==================== D13: can_reinit 推荐矩阵严格符合安全策略 ====================
    def test_D13_can_reinit_decision_matrix(self):
        """D13: 验证完整策略矩阵：仅 SIGNATURE_ERROR、CREDENTIAL_CORRUPTED、DPAPI_DECRYPT_FAILED 推荐重初始化"""
        # 允许重初始化
        self.assertTrue(diagnose_auth_error(BusinessAuthError("50004 签名错误", code=50004)).can_reinit)
        self.assertTrue(diagnose_auth_error(CredentialError("未找到凭据文件")).can_reinit)
        self.assertTrue(diagnose_auth_error(CredentialError("DPAPI 解密失败")).can_reinit)

        # 明确拒绝推荐重初始化
        self.assertFalse(diagnose_auth_error(BusinessAuthError("密码错误", code=10002)).can_reinit)
        self.assertFalse(diagnose_auth_error(AuthNetworkError("超时")).can_reinit)
        self.assertFalse(diagnose_auth_error(AuthNetworkError("500", http_status=500)).can_reinit)
        self.assertFalse(diagnose_auth_error(AuthNetworkError("429", http_status=429)).can_reinit)
        self.assertFalse(diagnose_auth_error(AuthNetworkError("401", http_status=401), context={"stage": "login"}).can_reinit)
        self.assertFalse(diagnose_auth_error(Exception("神秘未知异常")).can_reinit)

    # ==================== D14: 敏感信息全方位脱敏校验 ====================
    def test_D14_security_masking_comprehensive(self):
        """D14: 验证所有诊断输出中绝对不泄漏密码明文、完整 Token、signDigital"""
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

        exc = BusinessAuthError(raw_evil_text, code=50004)
        report = diagnose_auth_error(exc, context={"account": "13800138000"})
        report_text = report.format_report(verbose=True)

        self.assertNotIn("SuperSecretPassword123!", report_text)
        self.assertNotIn("74a9840283f5091741639d4b68e9d0fa734b", report_text)
        self.assertNotIn("evilpayload", report_text)
        self.assertIn("138****8000", report_text)

    # ==================== D15: 诊断过程无额外网络请求 ====================
    def test_D15_diagnostic_causes_no_network_requests(self):
        """D15: 验证调用诊断函数不会触发任何网络请求（纯只读离线分析）"""
        exc = BusinessAuthError("50004 签名错误", code=50004)
        with patch("urllib.request.urlopen") as mock_urlopen:
            report = diagnose_auth_error(exc)
            self.assertIsNotNone(report)
            mock_urlopen.assert_not_called()

    # ==================== D16: 交互与非交互模式处理 ====================
    def test_D16_handle_login_failure_modes(self):
        """D16: 验证 handle_login_failure 在非交互式终端下静默输出并在交互式下响应选项"""
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

    # ==================== D17: tools/login_cli.py 集成诊断无缝协同 ====================
    def test_D17_login_cli_integration_with_diagnostics(self):
        """D17: 当 login_cli 发生异常时，统一触发自动诊断报告输出"""
        import tools.login_cli as login_cli
        storage = CredentialStorage(self.cred_path)
        storage.save_credential("13800138000", "dummy_sig_123", "1", use_dpapi=False)

        stdout_buf = io.StringIO()
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
