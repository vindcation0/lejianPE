"""
乐健体育 · 首次配置向导与环境自检系统 (ConfigWizard).
=====================================================
涵盖全部 7 类用户启动状态的智能识别与差异化引导：
1. NO_CONFIG: 完全没有配置 -> 从头开始向导引导
2. CRED_WITHOUT_STATE: 有 credential，没有 state -> 仅登录初始化会话，免抓包
3. READY: 有有效 credential 和 state -> 跳过初始化，直接进入系统
4. CRED_CORRUPTED: credential 损坏或无法解密 -> 引导重新初始化修复
5. TOKEN_EXPIRED_REFRESHABLE: Token 过期但 RefreshToken 有效 -> 优先静默刷新
6. SESSION_EXPIRED: RefreshToken 已失效 -> 引导重新登录补全会话
7. PENDING_TRANSACTION: 上次初始化中途退出 -> 检查并自动恢复未决事务
"""
import datetime
import getpass
import json
import os
import shutil
import sqlite3
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

from core.auth_diagnostics import diagnose_auth_error
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
from core.auth_initializer import (
    DEFAULT_CRED_FILE,
    DEFAULT_FLOWS_FILE,
    DEFAULT_STATE_FILE,
    AuthInitializer,
)
from core.auth_manager import BASE_URL, AuthManager
from core.credential_storage import (
    CredentialStorage,
    is_windows,
    mask_account,
    mask_secret,
    secure_file_permissions,
)


class SystemSetupState(Enum):
    """系统就绪状态枚举"""
    NO_CONFIG = "NO_CONFIG"                                  # 1. 完全没有配置
    CRED_WITHOUT_STATE = "CRED_WITHOUT_STATE"                # 2. 有 credential，没有 state
    READY = "READY"                                          # 3. 有有效 credential 和 state
    CRED_CORRUPTED = "CRED_CORRUPTED"                        # 4. credential 损坏
    TOKEN_EXPIRED_REFRESHABLE = "TOKEN_EXPIRED_REFRESHABLE"  # 5. Token 过期但 RefreshToken 有效
    SESSION_EXPIRED = "SESSION_EXPIRED"                      # 6. RefreshToken 失效
    PENDING_TRANSACTION = "PENDING_TRANSACTION"              # 7. 上次初始化中途退出


class ConfigWizard:
    """
    负责新用户首次配置向导与环境自检。
    """
    _singleflight_lock = threading.Lock()
    _last_failure_time: float = 0.0
    _last_failure_reason: str = ""
    COOLDOWN_SECONDS: float = 10.0

    def __init__(
        self,
        cred_path: str = DEFAULT_CRED_FILE,
        state_path: str = DEFAULT_STATE_FILE,
        flows_path: str = DEFAULT_FLOWS_FILE,
        base_url: str = BASE_URL,
        use_dpapi: Optional[bool] = None,
    ):
        self.cred_path = os.path.abspath(cred_path)
        self.state_path = os.path.abspath(state_path)
        self.flows_path = os.path.abspath(flows_path)
        self.base_url = base_url
        self.use_dpapi = use_dpapi
        self.initializer = AuthInitializer(
            cred_path=self.cred_path,
            state_path=self.state_path,
            base_url=self.base_url,
            use_dpapi=self.use_dpapi,
        )
        self.auth_manager = self.initializer.auth_manager

    # =========================================================================
    # 一、状态检测器：覆盖 7 类用户启动状态
    # =========================================================================
    def detect_system_state(self) -> Dict[str, Any]:
        """
        全面检测当前系统就绪状态，精准判定为 7 种状态之一。
        返回结构化字典：
          - state: SystemSetupState
          - details: 文字说明
          - account: 账号（若可获取）
          - account_masked: 脱敏账号
          - 各自特定的标志位与上下文元数据
        """
        # 1. 优先检测未决事务 (PENDING_TRANSACTION)
        if os.path.exists(self.initializer.tx_path):
            try:
                with open(self.initializer.tx_path, "r", encoding="utf-8") as f:
                    tx_data = json.load(f)
                if tx_data.get("status") != "COMMITTED":
                    return {
                        "state": SystemSetupState.PENDING_TRANSACTION,
                        "details": "检测到上次认证初始化中途异常退出，存在未决事务",
                        "account": None,
                        "account_masked": "未知",
                        "can_auto_recover": True,
                    }
            except Exception:
                return {
                    "state": SystemSetupState.PENDING_TRANSACTION,
                    "details": "检测到未决事务文件损坏",
                    "account": None,
                    "account_masked": "未知",
                    "can_auto_recover": True,
                }

        # 2. 检查凭据文件
        cred_exists = os.path.exists(self.cred_path)
        cred_loaded: Optional[Dict[str, str]] = None
        cred_account: Optional[str] = None
        cred_err: Optional[str] = None

        if cred_exists:
            try:
                cred_loaded = self.initializer.storage.load_credential()
                cred_account = cred_loaded.get("account")
            except Exception as e:
                cred_err = str(e)
                return {
                    "state": SystemSetupState.CRED_CORRUPTED,
                    "details": f"凭据文件损坏或解密失败: {cred_err}",
                    "account": None,
                    "account_masked": "未知",
                    "can_reinit": True,
                    "error": cred_err,
                }

        # 3. 检查会话文件
        state_exists = os.path.exists(self.state_path)
        st_data: Optional[Dict[str, Any]] = None
        if state_exists:
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    loaded_json = json.load(f)
                if isinstance(loaded_json, dict):
                    st_data = loaded_json
            except Exception:
                st_data = None

        # 判定 A: 完全没有配置 (NO_CONFIG)
        if not cred_exists or not cred_loaded:
            return {
                "state": SystemSetupState.NO_CONFIG,
                "details": "本地尚未配置任何认证凭据 (credential.json 不存在)",
                "account": None,
                "account_masked": "未配置",
                "can_reinit": True,
            }

        acc_masked = mask_account(cred_account) if cred_account else "未配置"

        # 判定 B: 有 credential，没有 state.json (CRED_WITHOUT_STATE)
        if not state_exists or not st_data:
            return {
                "state": SystemSetupState.CRED_WITHOUT_STATE,
                "details": "已有有效认证凭据，但本地缺少有效会话 (state.json 不存在或格式损坏)",
                "account": cred_account,
                "account_masked": acc_masked,
                "cred_data": cred_loaded,
                "can_login": True,
            }

        # 判定 C: 检验会话字段与 Token 有效期
        access_token = st_data.get("accessToken")
        refresh_token = st_data.get("refreshToken")
        school_id = st_data.get("schoolId")
        semester_id = st_data.get("semesterId")
        exp_ms = int(st_data.get("accessTokenExpires") or 0)
        now_ms = time.time() * 1000

        # 如果 state 缺少核心必要字段
        if not access_token or not school_id or not semester_id:
            return {
                "state": SystemSetupState.CRED_WITHOUT_STATE,
                "details": "本地会话数据残缺（缺失 Token、学校或学期标识）",
                "account": cred_account,
                "account_masked": acc_masked,
                "cred_data": cred_loaded,
                "can_login": True,
            }

        token_expired = (exp_ms > 0 and now_ms >= exp_ms) or (not access_token)

        # 判定 D: 有有效 credential 和 state (READY)
        if not token_expired:
            exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp_ms / 1000)) if exp_ms > 0 else "未知"
            return {
                "state": SystemSetupState.READY,
                "details": f"认证凭据与会话均有效 (Token 有效期至 {exp_str})",
                "account": cred_account,
                "account_masked": acc_masked,
                "school_id": school_id,
                "semester_id": semester_id,
                "expires_str": exp_str,
                "can_proceed": True,
            }

        # 判定 E: Token 过期但 RefreshToken 有效 (TOKEN_EXPIRED_REFRESHABLE)
        if refresh_token and str(refresh_token).strip():
            return {
                "state": SystemSetupState.TOKEN_EXPIRED_REFRESHABLE,
                "details": "会话 Token 已过期，但 RefreshToken 存在，可优先尝试静默刷新",
                "account": cred_account,
                "account_masked": acc_masked,
                "school_id": school_id,
                "semester_id": semester_id,
                "refresh_token": refresh_token,
                "can_refresh": True,
            }

        # 判定 F: RefreshToken 失效或缺失 (SESSION_EXPIRED)
        return {
            "state": SystemSetupState.SESSION_EXPIRED,
            "details": "会话 Token 已过期且缺失有效 RefreshToken，需要重新登录",
            "account": cred_account,
            "account_masked": acc_masked,
            "school_id": school_id,
            "semester_id": semester_id,
            "can_login": True,
        }

    # =========================================================================
    # 二、环境自检：全面检查软硬件运行依赖
    # =========================================================================
    def check_environment(self) -> Dict[str, Any]:
        """
        全面检查系统环境：
        1. Python 版本 (>= 3.8)
        2. 操作系统与安全存储支持 (Windows DPAPI / POSIX)
        3. 必要模块可用性 (sqlite3, ssl, json, ctypes)
        4. 工作目录与文件可写性 (data, logs, captures, 项目根目录)
        5. SQLite 数据库连通性
        6. 抓包 ADB 工具可用性探测 (非致命，仅提示)
        """
        py_ver = sys.version_info
        py_ok = py_ver >= (3, 8)
        py_desc = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"

        os_name = sys.platform
        dpapi_supported = is_windows()

        # 检查目录与写权限
        dirs_to_check = [
            ("项目根目录", ROOT),
            ("数据目录 (data)", os.path.join(ROOT, "data")),
            ("日志目录 (logs)", os.path.join(ROOT, "logs")),
            ("抓包目录 (captures)", os.path.join(ROOT, "captures")),
        ]
        dir_results = []
        all_dirs_ok = True
        for name, p in dirs_to_check:
            try:
                os.makedirs(p, exist_ok=True)
                test_file = os.path.join(p, f".perm_test_{os.getpid()}_{time.time_ns()}.tmp")
                with open(test_file, "w", encoding="utf-8") as f:
                    f.write("test")
                os.remove(test_file)
                dir_results.append((name, True, "可读写"))
            except Exception as e:
                dir_results.append((name, False, f"不可写: {e}"))
                all_dirs_ok = False

        # SQLite 检查
        sqlite_ok = False
        sqlite_desc = ""
        try:
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE _test (id INT)")
            conn.close()
            sqlite_ok = True
            sqlite_desc = "正常 (内置 sqlite3 驱动就绪)"
        except Exception as e:
            sqlite_ok = False
            sqlite_desc = f"异常: {e}"

        # ADB 探测 (调用 capture_login.find_adb 或 shutil.which)
        adb_found = False
        adb_path = None
        try:
            import tools.capture_login as capture_login
            adb_path = capture_login.find_adb()
            adb_found = bool(adb_path)
        except Exception:
            adb_path = shutil.which("adb")
            adb_found = bool(adb_path)

        # 6. 网络连通性探测 (向乐健服务器发送轻量探测请求，非阻断式)
        network_ok = False
        network_desc = ""
        try:
            probe_url = self.base_url.rstrip("/") + "/"
            req = urllib.request.Request(probe_url, method="GET")
            req.add_header("User-Agent", "okhttp/3.12.1")
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=3.0, context=ctx) as resp:
                network_ok = True
                network_desc = f"畅通 (连接正常, HTTP {resp.status})"
        except urllib.error.HTTPError as e:
            network_ok = True
            network_desc = f"畅通 (服务端响应 HTTP {e.code})"
        except urllib.error.URLError as e:
            network_ok = False
            network_desc = f"受限或未连接 ({e.reason})"
        except Exception as e:
            network_ok = False
            network_desc = f"检测异常 ({e})"

        # 注意：网络连接为非阻断式检测（离线环境下仍可查看本地课表与配置）
        return {
            "python_ok": py_ok,
            "python_version": py_desc,
            "is_windows": dpapi_supported,
            "dpapi_supported": dpapi_supported,
            "dirs_ok": all_dirs_ok,
            "dirs_details": dir_results,
            "sqlite_ok": sqlite_ok,
            "sqlite_desc": sqlite_desc,
            "adb_found": adb_found,
            "adb_path": adb_path or "未检测到 (若已有凭据则无需模拟器)",
            "network_ok": network_ok,
            "network_desc": network_desc,
            "overall_ok": py_ok and all_dirs_ok and sqlite_ok,
        }

    def format_environment_report(self, env_report: Dict[str, Any]) -> str:
        """格式化环境自检报告"""
        lines = [
            "=" * 55,
            "                 环境自检报告",
            "=" * 55,
            f"Python 版本  : {env_report['python_version']} ({'符合要求' if env_report['python_ok'] else '过低，建议 >= 3.8'})",
            f"运行平台     : {sys.platform} ({'支持 Windows DPAPI 凭据安全保护' if env_report['dpapi_supported'] else 'POSIX 受限权限保护模式'})",
            f"SQLite 驱动  : {env_report['sqlite_desc']}",
            f"网络连通性   : {env_report['network_desc']}",
            f"安卓 ADB 工具: {env_report['adb_path']}",
            "-" * 55,
            "目录读写权限 :",
        ]
        for name, ok, desc in env_report["dirs_details"]:
            status_icon = "✓" if ok else "✗"
            lines.append(f"  [{status_icon}] {name:<18} : {desc}")
        lines.append("=" * 55)
        return "\n".join(lines)

    # =========================================================================
    # 三、静默刷新 Token (针对 TOKEN_EXPIRED_REFRESHABLE)
    # =========================================================================
    def silent_refresh_token(self) -> Tuple[bool, Optional[str]]:
        """
        使用现有 state.json 中的 RefreshToken 静默延长会话。
        采用进程级 Single-flight 单飞互斥并发控制与 Double-Checked Locking：
        1. 多个并发请求排队，仅首个请求穿透向服务端发起刷新调用；
        2. 后续请求获得锁后直接检测复用新鲜 Token，防范 RefreshToken 轮换冲突与重复写盘；
        3. 失败时触发冷却保护，防止短时间内无限重试打满接口。
        """
        with self._singleflight_lock:
            # 1. 检查失败冷却保护
            now = time.time()
            if (now - ConfigWizard._last_failure_time) < ConfigWizard.COOLDOWN_SECONDS:
                return False, f"刷新冷却保护中（请勿频繁重试）: {ConfigWizard._last_failure_reason}"

            if not os.path.exists(self.state_path):
                return False, "state.json 不存在"

            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    st = json.load(f)
            except Exception as e:
                return False, f"读取 state.json 失败: {e}"

            # 2. 双重检查 (Double-Checked Locking)：确认前序并发是否已经刚完成过刷新
            curr_exp = int(st.get("accessTokenExpires") or 0)
            if curr_exp > (time.time() * 1000 + 30000) and st.get("accessToken"):
                # 前序请求已经成功刷新过了，直接复用
                exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(curr_exp / 1000))
                return True, exp_str

            rt = st.get("refreshToken")
            if not rt or not str(rt).strip():
                return False, "缺少 refreshToken"

            url = urllib.parse.urljoin(self.base_url, "authorization/user/refreshToken")
            url_with_param = f"{url}?refreshToken={urllib.parse.quote(str(rt).strip())}"
            req = urllib.request.Request(url_with_param, method="GET")
            req.add_header("User-Agent", "okhttp/3.12.1")

            ctx = ssl.create_default_context()
            try:
                with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                    code = resp.status
                    raw = resp.read().decode("utf-8", "ignore")
            except urllib.error.HTTPError as e:
                err_msg = f"服务端拒绝刷新 (HTTP {e.code})"
                ConfigWizard._last_failure_time = time.time()
                ConfigWizard._last_failure_reason = err_msg
                return False, err_msg
            except Exception as e:
                err_msg = f"网络异常: {e}"
                ConfigWizard._last_failure_time = time.time()
                ConfigWizard._last_failure_reason = err_msg
                return False, err_msg

            try:
                res_json = json.loads(raw)
            except Exception:
                err_msg = "服务端响应非合法 JSON"
                ConfigWizard._last_failure_time = time.time()
                ConfigWizard._last_failure_reason = err_msg
                return False, err_msg

            if code != 200 or not isinstance(res_json, dict) or res_json.get("code") != 0:
                msg = res_json.get("message") if isinstance(res_json, dict) else raw[:100]
                err_msg = f"刷新失败 (code={res_json.get('code') if isinstance(res_json, dict) else code}): {msg}"
                ConfigWizard._last_failure_time = time.time()
                ConfigWizard._last_failure_reason = err_msg
                return False, err_msg

            data = res_json.get("data")
            if not isinstance(data, dict):
                return False, "刷新响应缺少 data 结构"

            new_tok = data.get("accessToken")
            new_rt = data.get("refreshToken") or rt
            new_exp = int(data.get("accessTokenExpires") or 0)

            if not new_tok or not str(new_tok).strip():
                return False, "响应中缺少 accessToken"

            new_state = dict(st)
            new_state["accessToken"] = str(new_tok).strip()
            new_state["refreshToken"] = str(new_rt).strip()
            new_state["accessTokenExpires"] = new_exp

            # 以原子安全方式写回
            try:
                self.auth_manager.save_state_atomically(
                    login_data=new_state,
                    state_path=self.state_path,
                    backup=True,
                )
                secure_file_permissions(self.state_path)
                ConfigWizard._last_failure_time = 0.0
                ConfigWizard._last_failure_reason = ""
                exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(new_exp / 1000)) if new_exp > 0 else "未知"
                return True, exp_str
            except Exception as e:
                return False, f"原子化保存刷新后的会话失败: {e}"

    # =========================================================================
    # 四、快速登录初始化会话 (针对 CRED_WITHOUT_STATE 与 SESSION_EXPIRED)
    # =========================================================================
    def quick_login_with_existing_credential(
        self,
        password: Optional[str] = None,
        interactive: bool = True,
    ) -> bool:
        """
        当本地已有有效 credential.json 时，免除模拟器抓包流程，
        直接输入密码进行命令行真实登录并原子更新 state.json。
        """
        try:
            cred = self.initializer.storage.load_credential()
        except Exception as e:
            print(f"❌ 读取本地凭据失败: {e}")
            return False

        account = cred["account"]
        sign_digital = cred["signDigital"]
        entrance = cred.get("entrance", "1")

        pwd = password
        if not pwd and interactive:
            print(f"\n检测到已有有效凭据配置：账号 【{mask_account(account)}】")
            print("无需重新抓包采集！仅需验证登录密码以初始化/更新会话。")
            try:
                pwd = getpass.getpass(f"请输入【{mask_account(account)}】的登录密码（不回显）：")
            except (KeyboardInterrupt, EOFError):
                print("\n操作已取消。")
                return False

        if not pwd or not str(pwd).strip():
            print("\n未输入密码，无法完成会话初始化。")
            return False

        print("\n正在向服务端提交登录验证…")
        try:
            login_data = self.auth_manager.login(
                account=account,
                password=pwd,
                sign_digital=sign_digital,
                entrance=entrance,
            )
            print("  登录验证通过！服务端已成功颁发会话。")
            print("正在同步更新本地会话 state.json…")
            saved_state = self.auth_manager.save_state_atomically(
                login_data=login_data,
                state_path=self.state_path,
                backup=True,
            )
            secure_file_permissions(self.state_path)
            print(f"  会话已成功落地！当前学期: {saved_state.get('semesterId') or '未知'}")
            print("\n========== 会话初始化完成 ==========")
            print(f"账号     : {mask_account(account)}")
            print(f"学校代码 : {login_data.get('schoolId')}")
            print(f"当前学期 : {saved_state.get('semesterId')}")
            print("状态     : 本地会话已更新就绪，可以直接使用系统。")
            print("===================================\n")
            return True
        except Exception as e:
            print("\n❌ 登录失败！")
            diag = diagnose_auth_error(e, context={"account": account, "stage": "quick_login"})
            print("\n" + diag.format_report(verbose=False))

            # 闭环分流：若错误可重初始化（如 50004 签名失效或修改过密码导致签名不匹配），提供一键重初始化入口
            if diag.can_reinit and interactive:
                print("-" * 55)
                print("💡 闭环引导：检测到当前凭据的签名可能已失效（例如修改了登录密码）。")
                print("   无需担心！可以通过模拟器重新登录官方 App 重新采集签名修复。")
                try:
                    choice = input("是否现在开始重新初始化认证？[1] 是 (默认) / [2] 否: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    return False
                if choice not in ("2", "n", "no", "q"):
                    return self.initializer.reinitialize(
                        password=pwd,
                        flows_path=self.flows_path,
                        use_dpapi=self.use_dpapi,
                        interactive=True,
                    )

            return False
        finally:
            # 及时释放局部变量引用以尽可能缩短密码在内存中的存活周期
            del pwd

    # =========================================================================
    # 五、首次配置向导主流程 (针对 NO_CONFIG)
    # =========================================================================
    def run_full_onboarding(
        self,
        account: Optional[str] = None,
        password: Optional[str] = None,
        capture_fn: Optional[Callable[[], bool]] = None,
        interactive: bool = True,
    ) -> bool:
        """
        全新用户的全流程引导：
        1. 欢迎页面与流程说明
        2. 环境自检与依赖确认
        3. 引导准备模拟器与官方 App
        4. 账号输入与预确认
        5. 调用 AuthInitializer 统一步骤完成抓包与 WAL 事务落盘
        6. 配置成功完成横幅与后续操作建议
        """
        if interactive:
            print("\n" + "=" * 55)
            print("             欢迎使用 lejianPE 首次配置向导")
            print("=" * 55)
            print("检测到当前为首次使用。本向导将带您完成：")
            print("  1. 运行环境自检")
            print("  2. 官方客户端与模拟器就绪确认")
            print("  3. 认证材料采集与在线登录验证")
            print("  4. 本地安全凭据与会话原子生成")
            print("-" * 55)
            print("【安全声明】")
            print("• 密码输入使用标准不回显机制，绝不以明文形式保存于磁盘或日志；")
            print("• 凭据在 Windows 下默认采用硬件 DPAPI 级安全加密保护；")
            print("• 采用 WAL 复合事务，任何步骤失败绝不留存残损配置。")
            print("=" * 55)
            try:
                input("\n按回车键开始环境自检…")
            except (KeyboardInterrupt, EOFError):
                print("\n向导已取消。")
                return False

        # 1. 环境自检
        env_res = self.check_environment()
        if interactive:
            print("\n" + self.format_environment_report(env_res))
            if not env_res["overall_ok"]:
                print("\n❌ 环境自检存在致命项未满足，请先解决上述错误后重新运行。")
                return False
            if not env_res["adb_found"]:
                print("💡 提示：未在 PATH 中检测到 adb 工具。请确认 MuMu 或雷电等安卓模拟器已安装并启动。")
            try:
                input("\n环境自检通过，按回车键进入官方客户端认证引导…")
            except (KeyboardInterrupt, EOFError):
                print("\n向导已取消。")
                return False

        # 2. 账号输入 (若未提供)
        target_account = account
        if not target_account and interactive:
            try:
                target_account = input("\n请输入您的乐健体育绑定手机号/账号：").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n向导已取消。")
                return False

        if not target_account:
            print("❌ 账号不能为空，向导中止。")
            return False

        # 3. 密码输入 (若未提供)
        pwd = password
        if not pwd and interactive:
            try:
                pwd = getpass.getpass(f"请输入【{mask_account(target_account)}】的登录密码（不回显）：")
            except (KeyboardInterrupt, EOFError):
                print("\n向导已取消。")
                return False

        if not pwd or not str(pwd).strip():
            print("❌ 密码不能为空，向导中止。")
            return False

        # 4. 调用 AuthInitializer 统一步骤完成重新初始化
        print("\n正在启动认证初始化流程…")
        try:
            ok = self.initializer.reinitialize(
                password=pwd,
                flows_path=self.flows_path,
                capture_fn=capture_fn,
                use_dpapi=self.use_dpapi,
                interactive=interactive,
            )
            if not ok:
                return False
        finally:
            del pwd

        # 5. 配置完成横幅与后续指引
        if interactive:
            print("\n" + "=" * 55)
            print("             🎉 恭喜！lejianPE 首次配置已全部完成")
            print("=" * 55)
            print(f"认证账号    : {mask_account(target_account)}")
            print("认证凭据    : 已安全生成 (credential.json)")
            print("本地会话    : 已成功建立 (state.json)")
            print("-" * 55)
            print("您现在可以：")
            print("  • 运行 python menu.py 进入主菜单")
            print("  • 运行 python lejian.py status 检查日常会话")
            print("  • 运行 python lejian.py timetable 查看课表与周次")
            print("  • 运行 python lejian.py list 查看当前可抢活动")
            print("=" * 55 + "\n")

        return True

    # =========================================================================
    # 六、综合调度统一入口 (run_wizard)
    # =========================================================================
    def run_wizard(
        self,
        interactive: bool = True,
        account: Optional[str] = None,
        password: Optional[str] = None,
        capture_fn: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """
        配置向导主入口：
        自动探测当前 7 类状态，并精准路由至对应的最优修复/引导分支。
        """
        # 第一步：状态侦测
        st = self.detect_system_state()
        state = st["state"]

        # 分支 1: PENDING_TRANSACTION (上次初始化中途退出)
        if state == SystemSetupState.PENDING_TRANSACTION:
            print("\n[事务守护] 检测到上次初始化异常退出，正在自动回滚并清理未决事务…")
            self.initializer.recover_pending_transaction()
            print("[事务守护] 事务已安全自愈。重新评估当前系统配置…")
            # 重新侦测
            st = self.detect_system_state()
            state = st["state"]

        # 分支 2: READY (已有完整有效配置)
        if state == SystemSetupState.READY:
            if interactive:
                print("\n" + "=" * 50)
                print("              系统配置已就绪")
                print("=" * 50)
                print(f"绑定账号    : {st['account_masked']}")
                print(f"学校代码    : {st['school_id']}")
                print(f"当前学期    : {st['semester_id']}")
                print(f"会话有效至  : {st['expires_str']}")
                print("-" * 50)
                print("所有凭据与会话均处于健康状态，无需重复配置！")
                print("=" * 50 + "\n")
            return True

        # 分支 3: TOKEN_EXPIRED_REFRESHABLE (Token 过期但 RefreshToken 有效)
        if state == SystemSetupState.TOKEN_EXPIRED_REFRESHABLE:
            if interactive:
                print(f"\n检测到账号【{st['account_masked']}】的 Token 已过期，正在尝试自动静默刷新…")
            ok, res_str = self.silent_refresh_token()
            if ok:
                if interactive:
                    print(f"✓ Token 自动刷新成功！会话已延长至 {res_str}，系统已完全就绪。")
                return True
            else:
                print(f"⚠️ RefreshToken 刷新未成功 ({res_str})，正在转入快速登录流程…")
                # 平滑降级为 CRED_WITHOUT_STATE / SESSION_EXPIRED
                state = SystemSetupState.SESSION_EXPIRED

        # 分支 4: CRED_WITHOUT_STATE 或 SESSION_EXPIRED (有凭据但无会话或会话失效)
        if state in (SystemSetupState.CRED_WITHOUT_STATE, SystemSetupState.SESSION_EXPIRED):
            return self.quick_login_with_existing_credential(
                password=password,
                interactive=interactive,
            )

        # 分支 5: CRED_CORRUPTED (凭据损坏)
        if state == SystemSetupState.CRED_CORRUPTED:
            if interactive:
                print("\n" + "=" * 50)
                print("             ⚠️ 本地凭据损坏警告")
                print("=" * 50)
                print(f"原因: {st.get('details')}")
                print("由于认证签名已损坏或无法解密，需要重新抓包初始化凭据。")
                print("-" * 50)
                try:
                    choice = input("是否现在开始重新初始化？[1] 是 (默认) / [2] 退出: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\n操作已取消。")
                    return False
                if choice in ("2", "n", "no", "q"):
                    print("已取消重新初始化。")
                    return False
            # 直接调用统一步骤初始化
            return self.initializer.reinitialize(
                password=password,
                flows_path=self.flows_path,
                capture_fn=capture_fn,
                use_dpapi=self.use_dpapi,
                interactive=interactive,
            )

        # 分支 6: NO_CONFIG (完全没有配置)
        if state == SystemSetupState.NO_CONFIG:
            return self.run_full_onboarding(
                account=account,
                password=password,
                capture_fn=capture_fn,
                interactive=interactive,
            )

        return False
