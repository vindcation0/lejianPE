"""
乐健体育 · 登录失败自动诊断模块 (Auth Diagnostics).
======================================================
职责：
1. 捕获认证全生命周期异常，将其结构化归一化为标准的 AuthDiagnosticReport。
2. 覆盖所有典型场景：签名失效(50004)、密码错误、登录401与业务401区分、Token 过期、
   网络异常、服务端5xx、请求限流(429)独立处理、AES解密失败、响应结构异常、凭据损坏、
   DPAPI平台限制与解密失败区分、未知错误兜底。
3. 严格安全脱敏：严禁泄露明文密码、完整 Token、完整 signDigital 及 AES 密钥。
4. 严格 Fail-closed 凭据安全：绝不自动降级或建议降级为明文存储。
5. 针对 429 限流提取 Retry-After 并采用保守等待策略，严禁自动连续重试。
6. 未知错误默认不推荐重新初始化，优先展示脱敏诊断报告供排查。
"""
import datetime
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

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
    UserInfoError,
)
from core.credential_storage import mask_account, mask_secret


class ErrorCategory:
    """认证错误标准分类枚举/常量"""
    SIGNATURE_ERROR = "SIGNATURE_ERROR"              # 业务码 50004 签名错误
    WRONG_PASSWORD = "WRONG_PASSWORD"                # 密码或账号错误
    UNAUTHORIZED = "UNAUTHORIZED"                    # 登录接口 HTTP 401 凭据无效或未授权
    TOKEN_EXPIRED = "TOKEN_EXPIRED"                  # 业务接口 HTTP 401 访问令牌过期或作废
    REFRESH_TOKEN_EXPIRED = "REFRESH_TOKEN_EXPIRED"  # 刷新凭据失效
    RATE_LIMITED = "RATE_LIMITED"                    # HTTP 429 服务端限流
    NETWORK_ERROR = "NETWORK_ERROR"                  # 网络/DNS/连接超时
    SERVER_ERROR = "SERVER_ERROR"                    # HTTP 5xx 故障
    CLIENT_PARAM_ERROR = "CLIENT_PARAM_ERROR"        # HTTP 4xx 请求参数错误或拒绝 (非 401/429)
    DECRYPT_FAILED = "DECRYPT_FAILED"                # AES 信封解密失败
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"        # 响应缺少必要字段
    CREDENTIAL_CORRUPTED = "CREDENTIAL_CORRUPTED"    # 本地凭据损坏或缺失
    DPAPI_UNAVAILABLE = "DPAPI_UNAVAILABLE"          # 平台不支持 Windows DPAPI 凭据安全机制
    DPAPI_DECRYPT_FAILED = "DPAPI_DECRYPT_FAILED"    # Windows DPAPI 凭据解密失败 (跨用户/密文损坏)
    UNKNOWN_ERROR = "UNKNOWN_ERROR"                  # 未知错误兜底


CATEGORY_NAMES = {
    ErrorCategory.SIGNATURE_ERROR: "认证签名错误 (50004)",
    ErrorCategory.WRONG_PASSWORD: "账号或密码错误",
    ErrorCategory.UNAUTHORIZED: "登录凭据无效或未授权 (HTTP 401)",
    ErrorCategory.TOKEN_EXPIRED: "业务会话已过期 (Token Expired, HTTP 401)",
    ErrorCategory.REFRESH_TOKEN_EXPIRED: "会话刷新凭据失效 (RefreshToken Expired)",
    ErrorCategory.RATE_LIMITED: "服务端请求限流 (HTTP 429)",
    ErrorCategory.NETWORK_ERROR: "网络连接通信异常",
    ErrorCategory.SERVER_ERROR: "服务端故障 (HTTP 5xx)",
    ErrorCategory.CLIENT_PARAM_ERROR: "请求参数错误或访问受限 (HTTP 4xx)",
    ErrorCategory.DECRYPT_FAILED: "登录信封解密失败",
    ErrorCategory.MALFORMED_RESPONSE: "服务端响应数据结构异常",
    ErrorCategory.CREDENTIAL_CORRUPTED: "本地认证凭据异常",
    ErrorCategory.DPAPI_UNAVAILABLE: "平台不支持 Windows DPAPI 凭据安全机制",
    ErrorCategory.DPAPI_DECRYPT_FAILED: "Windows DPAPI 凭据解密失败",
    ErrorCategory.UNKNOWN_ERROR: "未分类未知异常",
}


def mask_sensitive_text(text: str) -> str:
    """
    对任意异常文本中的敏感信息进行脱敏处理：
    - 去除长 Hex 字符串或 Base64 签名串
    - 去除 Bearer Token
    - 去除明显的密码明文字段
    """
    if not text:
        return ""
    s = str(text)

    # 1. 屏蔽 Bearer token (JWT 格式或通用长字符)
    s = re.sub(
        r"(Bearer\s+)[a-zA-Z0-9_\-\.]{15,}",
        r"\1[MASKED_TOKEN]",
        s,
        flags=re.IGNORECASE,
    )

    # 2. 屏蔽 signDigital / 签名相关长字符串 (>= 20 字符的 hex 或 base64)
    s = re.sub(
        r"(signDigital[\"':\s=]+)([a-zA-Z0-9_\+/=]{20,})",
        r"\1[MASKED_SIGNATURE]",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(
        r"(signDigitalEncrypted[\"':\s=]+)([a-zA-Z0-9_\+/=]{20,})",
        r"\1[MASKED_CIPHERTEXT]",
        s,
        flags=re.IGNORECASE,
    )

    # 3. 屏蔽 password 明文
    s = re.sub(
        r"(password[\"':\s=]+)[^\s,}\"]+",
        r"\1[PROTECTED]",
        s,
        flags=re.IGNORECASE,
    )

    # 4. 屏蔽独立出现的超长 token 字符串 (形如 eyJhbGci...)
    s = re.sub(
        r"eyJ[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{20,}\.[a-zA-Z0-9_\-]{10,}",
        r"[MASKED_JWT_TOKEN]",
        s,
    )

    return s


class AuthDiagnosticReport:
    """
    结构化认证诊断报告。
    """

    def __init__(
        self,
        diagnostic_id: str,
        timestamp: str,
        error_category: str,
        category_name: str,
        summary: str,
        possible_causes: List[str],
        suggested_actions: List[str],
        http_status: Optional[int] = None,
        business_code: Optional[int] = None,
        account_masked: Optional[str] = None,
        can_reinit: bool = False,
        can_retry: bool = False,
        retry_after: Optional[int] = None,
        raw_message_masked: str = "",
    ):
        self.diagnostic_id = diagnostic_id
        self.timestamp = timestamp
        self.error_category = error_category
        self.category_name = category_name
        self.summary = summary
        self.possible_causes = list(possible_causes)
        self.suggested_actions = list(suggested_actions)
        self.http_status = http_status
        self.business_code = business_code
        self.account_masked = account_masked
        self.can_reinit = can_reinit
        self.can_retry = can_retry
        self.retry_after = retry_after
        self.raw_message_masked = raw_message_masked

    def to_dict(self) -> Dict[str, Any]:
        """导出结构化字典（严格脱敏）"""
        return {
            "diagnostic_id": self.diagnostic_id,
            "timestamp": self.timestamp,
            "error_category": self.error_category,
            "category_name": self.category_name,
            "summary": self.summary,
            "http_status": self.http_status,
            "business_code": self.business_code,
            "account_masked": self.account_masked,
            "possible_causes": self.possible_causes,
            "suggested_actions": self.suggested_actions,
            "can_reinit": self.can_reinit,
            "can_retry": self.can_retry,
            "retry_after": self.retry_after,
            "raw_message_masked": self.raw_message_masked,
        }

    def format_report(self, verbose: bool = False) -> str:
        """
        格式化输出人类可读的终端诊断报告。
        """
        lines = [
            "=" * 50,
            "                ========== 登录诊断 ==========",
            "=" * 50,
            f"诊断编号：{self.diagnostic_id}",
            f"诊断时间：{self.timestamp}",
            "登录结果：失败",
        ]

        if self.account_masked:
            lines.append(f"操作账号：{self.account_masked}")

        lines.append(f"错误类型：{self.category_name}")

        code_parts = []
        if self.business_code is not None and self.business_code != -1:
            code_parts.append(f"业务代码 {self.business_code}")
        if self.http_status is not None and self.http_status != -1:
            code_parts.append(f"HTTP {self.http_status}")
        if self.retry_after is not None:
            code_parts.append(f"Retry-After {self.retry_after}s")
        if code_parts:
            lines.append(f"服务端响应代码：{', '.join(code_parts)}")

        lines.append("")
        lines.append(f"问题摘要：{self.summary}")
        lines.append("")
        lines.append("可能原因：")
        for idx, cause in enumerate(self.possible_causes, start=1):
            lines.append(f"  {idx}. {cause}")

        lines.append("")
        lines.append("建议操作：")
        for idx, action in enumerate(self.suggested_actions, start=1):
            lines.append(f"  {idx}. {action}")

        if verbose and self.raw_message_masked:
            lines.append("")
            lines.append("-" * 50)
            lines.append("底层脱敏诊断详情：")
            lines.append(f"  {self.raw_message_masked}")

        lines.append("=" * 50)
        return "\n".join(lines)


def _generate_diag_id() -> str:
    """生成带时间戳和短哈希的唯一诊断编号"""
    t_str = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    short_hash = f"{int(time.time() * 1000) % 10000:04d}"
    return f"DIAG-AUTH-{t_str}-{short_hash}"


def diagnose_auth_error(
    exc: Exception,
    context: Optional[Dict[str, Any]] = None,
) -> AuthDiagnosticReport:
    """
    根据捕获的异常和上下文，生成统一的认证诊断报告。
    安全保证：
    - 不会发送任何网络请求。
    - 绝不记录或返回明文敏感凭据。
    - 严格遵循 Fail-closed 安全策略，禁止降级明文存储。
    - 区分登录 401 与业务 401。
    - 429 独立处理并提取 Retry-After，禁止连续重试。
    - 未知错误严格不推荐重新初始化，优先展示脱敏报告供排查。
    """
    ctx = context or {}
    account = ctx.get("account")
    stage = ctx.get("stage") or "login"

    account_masked = mask_account(account) if account else None
    diag_id = _generate_diag_id()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    raw_msg = mask_sensitive_text(str(exc))

    http_status = ctx.get("http_status")
    business_code = ctx.get("business_code")

    if hasattr(exc, "http_status"):
        http_status = getattr(exc, "http_status")
    if hasattr(exc, "code"):
        business_code = getattr(exc, "code")

    # =========================================================================
    # 1. 本地凭据异常 (CredentialError / InvalidCredentialError)
    # =========================================================================
    if isinstance(exc, (CredentialError, InvalidCredentialError)):
        # 1.1 平台不支持 Windows DPAPI 凭据安全机制
        if "非 windows" in raw_msg.lower() or "notimplementederror" in raw_msg.lower() or "仅在 windows" in raw_msg:
            category = ErrorCategory.DPAPI_UNAVAILABLE
            summary = "当前操作系统平台不支持 Windows DPAPI 凭据安全加密体系。"
            causes = [
                "当前运行环境非 Windows，系统缺少原生 DPAPI (CryptProtectData) 安全隔离机制。",
                "系统遵循 Fail-closed 安全屏障，严禁在不支持 DPAPI 的环境自动降级为明文存储凭据。",
            ]
            actions = [
                "请在受信任的 Windows 环境下运行以保证凭据加密存储安全。",
                "严格禁止降级为明文凭据保存，防止敏感认证材料外泄。",
                "若需在非 Windows 平台部署，应配置跨平台安全凭据隔离方案（如 Keyring/安全密钥库）。",
            ]
            return AuthDiagnosticReport(
                diagnostic_id=diag_id,
                timestamp=now_str,
                error_category=category,
                category_name=CATEGORY_NAMES[category],
                summary=summary,
                possible_causes=causes,
                suggested_actions=actions,
                http_status=http_status,
                business_code=business_code,
                account_masked=account_masked,
                can_reinit=False,  # 非 Windows 下重抓也无法调用 DPAPI，先明确系统限制
                can_retry=False,
                raw_message_masked=raw_msg,
            )

        # 1.2 Windows DPAPI 解密失败 (跨用户/密文损坏/主密钥失效)
        if ("dpapi" in raw_msg.lower() or "crypt" in raw_msg.lower() or "无权访问" in raw_msg) and ("解密" in raw_msg or "失败" in raw_msg):
            category = ErrorCategory.DPAPI_DECRYPT_FAILED
            summary = "Windows DPAPI 凭据解密失败（Fail-closed 拒绝采用不可信或无权数据）。"
            causes = [
                "当前 credential.json 是由其他 Windows 用户账号加密创建，当前用户无权限解密。",
                "本地凭据中的 DPAPI 密文字段已损坏或遭篡改。",
                "操作系统用户登录密码近期发生重置，导致 DPAPI 主解密密钥失效。",
            ]
            actions = [
                "请在当前 Windows 登录用户下重新执行认证初始化（主菜单 [b]），重新生成属于当前账户的加密凭据。",
                "系统已执行 Fail-closed 保护，原凭据完好无损，未发生破坏或覆盖。",
            ]
            return AuthDiagnosticReport(
                diagnostic_id=diag_id,
                timestamp=now_str,
                error_category=category,
                category_name=CATEGORY_NAMES[category],
                summary=summary,
                possible_causes=causes,
                suggested_actions=actions,
                http_status=http_status,
                business_code=business_code,
                account_masked=account_masked,
                can_reinit=True,  # 允许在当前账户下一键重新初始化生成专属凭据
                can_retry=False,
                raw_message_masked=raw_msg,
            )

        # 1.3 本地凭据损坏或缺失 (credential.json 格式或字段异常)
        category = ErrorCategory.CREDENTIAL_CORRUPTED
        summary = "本地认证凭据 (credential.json) 缺失、损坏或内容不完整。"
        causes = [
            "首次使用，尚未生成本地 credential.json 凭据文件。",
            "credential.json 格式损坏（非标准 JSON）或必要字段缺失（缺少 signDigital 或账号）。",
        ]
        actions = [
            "请执行认证凭据初始化：在菜单中选择 [b] 重新抓取登录签名。",
            "或根据官方抓包指引重新生成 credential.json。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=True,  # 推荐重新初始化
            can_retry=False,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 2. 签名错误识别：业务码 50004 (在线服务端认证)
    # =========================================================================
    if (business_code == 50004) or ("50004" in raw_msg) or ("签名" in raw_msg and isinstance(exc, (BusinessAuthError, AuthError))):
        category = ErrorCategory.SIGNATURE_ERROR
        summary = "服务端拒绝认证请求：客户端提供的登录签名 (signDigital) 校验失败。"
        causes = [
            "账号在官方 App 端曾修改过密码（导致签名绑定的密码哈希失效）。",
            "本地 credential.json 中保存的 signDigital 已经失效或过期。",
            "客户端登录入口 (entrance) 或认证上下文参数发生变更。",
        ]
        actions = [
            "重新初始化认证凭据：使用主菜单 [b] 重新抓取登录签名。",
            "若曾在 App 端修改过密码，请确保在重新抓包时使用最新密码登录。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status or 200,
            business_code=business_code if business_code is not None else 50004,
            account_masked=account_masked,
            can_reinit=True,  # 推荐重新初始化
            can_retry=False,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 3. 密码或账号错误识别
    # =========================================================================
    is_wrong_pwd = False
    if isinstance(exc, BusinessAuthError):
        if ("密码" in raw_msg or "账号" in raw_msg or "用户" in raw_msg) and "签名" not in raw_msg:
            is_wrong_pwd = True
        elif business_code in (10001, 10002):
            is_wrong_pwd = True
    elif "密码错误" in raw_msg or "账号或密码" in raw_msg:
        is_wrong_pwd = True

    if is_wrong_pwd:
        category = ErrorCategory.WRONG_PASSWORD
        summary = "服务端提示账号或密码不正确。"
        causes = [
            "输入的密码有误（请注意大小写及特殊字符输入）。",
            "账号输入有误或该账号在当前学校未开通。",
        ]
        actions = [
            "请仔细核对账号和密码后重新输入。",
            "若忘记密码，请先在乐健体育官方 App 完成密码重置。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status or 200,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,  # 严禁引导重新抓取签名
            can_retry=True,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 4. HTTP 429 请求限流信号 (独立分类，保守等待，严禁连续重试)
    # =========================================================================
    if http_status == 429 or "429" in raw_msg or "too many requests" in raw_msg.lower():
        category = ErrorCategory.RATE_LIMITED

        # 尝试提取 Retry-After
        retry_after_val: Optional[int] = None
        if ctx.get("retry_after"):
            try:
                retry_after_val = int(ctx.get("retry_after"))
            except Exception:
                pass
        elif ctx.get("headers"):
            hdrs = ctx.get("headers", {})
            val = hdrs.get("Retry-After") or hdrs.get("retry-after")
            if val:
                try:
                    retry_after_val = int(val)
                except Exception:
                    pass
        if retry_after_val is None:
            m = re.search(r"retry-after[:\s]+(\d+)", raw_msg, re.IGNORECASE)
            if m:
                try:
                    retry_after_val = int(m.group(1))
                except Exception:
                    pass

        if retry_after_val is not None and retry_after_val > 0:
            summary = f"服务端触发频控限流保护 (HTTP 429)，服务端指示冷却等待 {retry_after_val} 秒。"
            wait_action = f"服务端返回了 Retry-After，请严格暂停所有请求并等待 {retry_after_val} 秒后再试。"
        else:
            summary = "服务端触发频控限流保护 (HTTP 429)，当前请求频次超出防刷阈值。"
            wait_action = "服务端未提供 Retry-After 标头，系统采用保守冷却策略，请暂停操作至少 60~180 秒后再试。"

        causes = [
            "在短时间内发起了过多登录或接口请求，触发了服务端的防刷安全拦截。",
            "当前网络出口 IP 处于服务端访问频次限制窗口期内。",
        ]
        actions = [
            wait_action,
            "严禁自动连续重试或高频刷新，以免 IP 遭到更长时间的封锁惩罚。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=429,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,  # 限流不是签名问题，不重新初始化
            can_retry=False,   # 严禁自动重试
            retry_after=retry_after_val,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 5. HTTP 401 细粒度区分：登录接口 401 vs 业务接口 401 (Token Expired)
    # =========================================================================
    is_401 = False
    if http_status == 401:
        is_401 = True
    elif isinstance(exc, AuthNetworkError) and exc.http_status == 401:
        is_401 = True
    elif "401" in raw_msg and ("未授权" in raw_msg or "unauthorized" in raw_msg.lower()):
        is_401 = True

    if is_401:
        is_login_endpoint = (
            stage == "login"
            or "manage/login" in raw_msg.lower()
            or "login" in str(ctx.get("path", "")).lower()
        )

        if is_login_endpoint:
            # 5.1 登录接口 401：凭据无效、未激活、无权限
            category = ErrorCategory.UNAUTHORIZED
            summary = "登录接口返回 HTTP 401 未授权：账号登录凭据未通过服务端核验。"
            causes = [
                "提交的登录认证材料已被服务端判定无效或撤销。",
                "账号状态异常（例如账号被冻结、尚未激活或未在学校系统完成注册）。",
                "登录入口 (entrance) 或认证通道无权访问该接口。",
            ]
            actions = [
                "请先在乐健体育官方 App 验证账号是否能正常登录并查看账号状态。",
                "核对账号在教务学籍系统的开通情况后再尝试登录。",
                "请勿连续高频发起登录请求。",
            ]
            return AuthDiagnosticReport(
                diagnostic_id=diag_id,
                timestamp=now_str,
                error_category=category,
                category_name=CATEGORY_NAMES[category],
                summary=summary,
                possible_causes=causes,
                suggested_actions=actions,
                http_status=401,
                business_code=business_code,
                account_masked=account_masked,
                can_reinit=False,  # 先检查账号状态，不盲目重抓
                can_retry=False,
                raw_message_masked=raw_msg,
            )
        else:
            # 5.2 业务接口 401：访问令牌过期或被注销
            category = ErrorCategory.TOKEN_EXPIRED
            summary = "业务接口返回 HTTP 401 未授权：当前访问令牌 (AccessToken) 已过期或已被作废。"
            causes = [
                "距离上次登录已超过 AccessToken 有效期（通常为数小时至数天）。",
                "在其他设备或官方客户端重复登录导致原 Token 被顶下线或撤回。",
            ]
            actions = [
                "执行命令行账号密码登录（主菜单 [9]），重新获取新的 Token。",
                "或尝试通过主菜单 [8] 刷新 Token。",
            ]
            return AuthDiagnosticReport(
                diagnostic_id=diag_id,
                timestamp=now_str,
                error_category=category,
                category_name=CATEGORY_NAMES[category],
                summary=summary,
                possible_causes=causes,
                suggested_actions=actions,
                http_status=401,
                business_code=business_code,
                account_masked=account_masked,
                can_reinit=False,
                can_retry=True,
                raw_message_masked=raw_msg,
            )

    # =========================================================================
    # 6. 会话 RefreshToken 失效识别
    # =========================================================================
    if "refreshtoken" in raw_msg.lower() or ("刷新" in raw_msg and ("失效" in raw_msg or "过期" in raw_msg)):
        category = ErrorCategory.REFRESH_TOKEN_EXPIRED
        summary = "长期刷新凭据 (RefreshToken) 已失效，无法自动刷新会话。"
        causes = [
            "RefreshToken 生命周期已到期（超出服务端允许的最长离线时限）。",
            "服务端会话状态被强制重置。",
        ]
        actions = [
            "使用主菜单 [9] 重新执行命令行账号密码登录。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,
            can_retry=True,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 7. 服务端故障 HTTP 5xx 识别
    # =========================================================================
    is_5xx = False
    if http_status and http_status >= 500:
        is_5xx = True
    elif any(f"HTTP {code}" in raw_msg for code in (500, 502, 503, 504)):
        is_5xx = True

    if is_5xx:
        category = ErrorCategory.SERVER_ERROR
        status_num = http_status if (http_status and http_status >= 500) else 500
        summary = f"乐健服务端基础设施出现异常 (HTTP {status_num})。"
        causes = [
            "乐健后台服务器发生内部故障或正在停机维护。",
            "上游网关或反向代理超时 (502 / 504)。",
        ]
        actions = [
            "服务端临时故障，建议稍等几分钟后重新尝试登录。",
            "请勿在短时间内高频重试，以免触发防御策略。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=status_num,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,  # 服务端故障严禁引导重抓签名
            can_retry=True,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 8. 请求参数错误或受限 (HTTP 4xx 非 401/429)
    # =========================================================================
    if (http_status and 400 <= http_status < 500) or ("403" in raw_msg):
        category = ErrorCategory.CLIENT_PARAM_ERROR
        if http_status == 403 or "403" in raw_msg:
            summary = "服务端拒绝访问：权限不足或 IP 受到策略限制 (HTTP 403)。"
            causes = [
                "请求遭到安全网关或 WAF 拦截。",
                "所使用的网络出口 IP 处于限制列表中。",
            ]
            actions = [
                "检查本地代理或切换到校园网/移动热点后重试。",
            ]
        else:
            summary = f"客户端请求参数有误 (HTTP {http_status or 400})。"
            causes = [
                "客户端发送的数据载荷格式与服务端当前版本要求不一致。",
            ]
            actions = [
                "请检查网络配置或查看最新版本更新说明。",
            ]

        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status or 400,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,
            can_retry=False,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 9. 网络连接通信异常 (DNS 失败、超时、断网等)
    # =========================================================================
    is_network = False
    if isinstance(exc, AuthNetworkError) and not is_5xx:
        is_network = True
    elif any(kw in raw_msg.lower() for kw in ("timeout", "timed out", "connection refused", "dns", "getaddrinfo", "nameresolution", "网络连接", "请求超时", "无法连接")):
        is_network = True

    if is_network:
        category = ErrorCategory.NETWORK_ERROR
        summary = "无法建立与乐健服务器的网络通信连接。"
        causes = [
            "本地网络未连接、Wi-Fi 掉线或信号极其微弱。",
            "DNS 解析 cpes.legym.cn 失败。",
            "系统全局代理（如 Fiddler / 抓包工具 / 科学上网工具）异常拦截了流量。",
        ]
        actions = [
            "检查本地网络连接是否顺畅（可在浏览器尝试访问 https://cpes.legym.cn）。",
            "检查系统代理设置，确认没有遗留未关闭的无效代理端口。",
            "排除网络故障后重试。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,  # 网络故障严禁引导重抓签名
            can_retry=True,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 10. 解密失败 (AES 登录信封解密异常)
    # =========================================================================
    if "解密" in raw_msg and ("信封" in raw_msg or "载荷" in raw_msg or "pyd" in raw_msg or "aes" in raw_msg.lower()):
        category = ErrorCategory.DECRYPT_FAILED
        summary = "服务端返回的加密载荷 (AES pyd) 解密失败。"
        causes = [
            "服务端响应数据被篡改、截断或格式发生协议变更。",
            "客户端动态密钥计算与服务端时间戳校验窗口存在偏差。",
        ]
        actions = [
            "系统已拒绝保存残缺会话，旧会话安全保留。",
            "请检查系统本地时间是否与网络北京时间准确同步。",
            "若持续出现，请保存诊断报告并向开发者反馈。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status or 200,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,
            can_retry=True,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 11. 响应缺少必要字段 / 结构异常
    # =========================================================================
    if isinstance(exc, (TokenAcquisitionError, SchoolInfoError, SemesterInfoError)) or ("缺少" in raw_msg and ("token" in raw_msg.lower() or "schoolid" in raw_msg.lower() or "data" in raw_msg.lower())):
        category = ErrorCategory.MALFORMED_RESPONSE
        summary = "服务端响应格式非法或缺失必要字段（如 Token 或学校标识）。"
        causes = [
            "服务端返回了业务错误但 HTTP 返回 200，且未包含有效 data。",
            "该账号首次使用或在学校教务系统中未完全绑定学生档案。",
            "服务端接口版本迭代，字段命名规则发生变更。",
        ]
        actions = [
            "系统已自动触发安全屏障，原 state.json 未被修改。",
            "请确认该账号在官方 App 中能正常显示课表和活动信息。",
        ]
        return AuthDiagnosticReport(
            diagnostic_id=diag_id,
            timestamp=now_str,
            error_category=category,
            category_name=CATEGORY_NAMES[category],
            summary=summary,
            possible_causes=causes,
            suggested_actions=actions,
            http_status=http_status or 200,
            business_code=business_code,
            account_masked=account_masked,
            can_reinit=False,
            can_retry=False,
            raw_message_masked=raw_msg,
        )

    # =========================================================================
    # 12. 未知错误安全兜底 (UNKNOWN_ERROR) - 严格不建议重新初始化，先诊断！
    # =========================================================================
    category = ErrorCategory.UNKNOWN_ERROR
    summary = "登录过程遇到未分类的未知异常。"
    causes = [
        "服务端返回了未收录的非标准响应或业务代码。",
        "运行时遭遇未预期的系统或环境异常。",
        "原因尚未完全确定，请优先查看下方脱敏诊断详情进行排查，切勿盲目重新初始化认证。",
    ]
    actions = [
        "查看脱敏诊断详情了解底层异常提示。",
        "确认服务端状态及网络环境，避免盲目重抓签名。",
        "若问题依然存在，请将诊断报告反馈给维护者。",
    ]
    return AuthDiagnosticReport(
        diagnostic_id=diag_id,
        timestamp=now_str,
        error_category=category,
        category_name=CATEGORY_NAMES[category],
        summary=summary,
        possible_causes=causes,
        suggested_actions=actions,
        http_status=http_status,
        business_code=business_code,
        account_masked=account_masked,
        can_reinit=False,  # 严格按用户要求设为 False！先诊断，不建议重初始化
        can_retry=False,
        raw_message_masked=raw_msg,
    )


def handle_login_failure(
    exc: Exception,
    context: Optional[Dict[str, Any]] = None,
    interactive: Optional[bool] = None,
    on_reinit: Optional[Callable[[], Any]] = None,
) -> AuthDiagnosticReport:
    """
    处理登录失败：
    1. 生成结构化脱敏诊断报告；
    2. 输出终端报告；
    3. 若为交互式终端，提供交互式选项引导用户选择重新初始化或查看详情。
    4. 若为非交互式模式，直接输出诊断并安全退出。
    """
    report = diagnose_auth_error(exc, context)
    print("\n" + report.format_report(verbose=False))

    is_interactive = interactive if interactive is not None else sys.stdin.isatty()

    if not is_interactive:
        return report

    # 交互式处理引导
    while True:
        print("\n请选择后续操作：")
        if report.can_reinit:
            print("  [1] 是，开始重新初始化认证凭据")
            print("  [2] 否，返回主菜单/退出")
        elif report.can_retry:
            print("  [1] 重新尝试登录")
            print("  [2] 返回主菜单/退出")
        else:
            print("  [1] 退出并稍后排查")
            print("  [2] 返回主菜单")
        print("  [3] 查看详细诊断信息（已脱敏）")

        try:
            choice = input("请选择 [1/2/3]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消操作。")
            break

        if choice == "1":
            if report.can_reinit:
                if on_reinit:
                    on_reinit()
                else:
                    print("\n提示：当前一键初始化功能可通过菜单 [b] 重新抓取签名，")
                    print("或运行：python tools\\capture_login.py")
            elif report.can_retry:
                print("\n请返回主菜单后重新尝试登录。")
            else:
                if report.error_category == ErrorCategory.RATE_LIMITED:
                    print("\n[注意] 当前处于服务端限流状态，请严格遵循冷却时间等待，切勿连续发起请求。")
                else:
                    print("\n已记录当前诊断编号，请排查原因后再试。")
            break
        elif choice in ("2", "q", "0"):
            break
        elif choice == "3":
            print("\n" + report.format_report(verbose=True))
        else:
            print("输入无效，请重新选择。")

    return report
