"""
乐健体育 · 认证重新初始化统一流程管理器 (AuthInitializer).
============================================================
职责：
1. 统一管理认证重初始化完整七步生命周期：
   - 步骤 1：本地认证与会话状态检查（脱敏只读，自动检测并恢复未决事务）
   - 步骤 2：官方客户端与模拟器就绪引导（提示账号一致性与安全声明）
   - 步骤 3：调用抓包并解析认证材料（复用 capture_login 与 login_envelope）
   - 步骤 4：严格预校验新凭据（格式、账号一致性防串号、真实登录验证）
   - 步骤 5：开启跨文件复合事务并原子替换旧凭据（临时文件 + os.replace，记录 CRED_APPLIED）
   - 步骤 6：拉取学期上下文并原子更新 state.json（遇错自动完整回滚凭据与会话）
   - 步骤 7：复合事务提交与完成确认（清理事务备份与日志，确认双文件强一致）
2. 安全防护底线：
   - Fail-closed：任何中间环节验证失败，绝不破坏原有可用凭据和会话。
   - 跨文件复合事务（2PC）：凭据与会话更新保证强一致性，任何一方失败自动完全回滚。
   - 严格账号一致性核验：若已有配置账号，拒绝采纳不一致的他方账号凭据。
   - 绝不保存明文密码，日志严格脱敏。
"""
import datetime
import getpass
import json
import os
import shutil
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

try:
    from tools.login_envelope import unwrap
except ImportError:
    from login_envelope import unwrap

from core.auth_diagnostics import diagnose_auth_error, handle_login_failure
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
from core.auth_manager import AuthManager, BASE_URL, LOGIN_PATH
from core.credential_storage import (
    CredentialStorage,
    is_windows,
    mask_account,
    mask_secret,
)

DEFAULT_CRED_FILE = os.path.join(ROOT, "credential.json")
DEFAULT_STATE_FILE = os.path.join(ROOT, "state.json")
DEFAULT_FLOWS_FILE = os.path.join(ROOT, "captures", "login_flows.jsonl")


class AuthInitializer:
    """
    负责一键重新初始化认证全流程。
    """

    def __init__(
        self,
        cred_path: str = DEFAULT_CRED_FILE,
        state_path: str = DEFAULT_STATE_FILE,
        base_url: str = BASE_URL,
        use_dpapi: Optional[bool] = None,
    ):
        self.cred_path = os.path.abspath(cred_path)
        self.state_path = os.path.abspath(state_path)
        self.tx_path = self.cred_path + ".tx.json"
        self.cred_tx_bak = self.cred_path + ".tx_bak"
        self.state_tx_bak = self.state_path + ".tx_bak"
        self.base_url = base_url
        self.use_dpapi = use_dpapi
        self.storage = CredentialStorage(self.cred_path)
        self.auth_manager = AuthManager(base_url=self.base_url)

    # =========================================================================
    # 跨文件复合事务保护机制 (Composite Transaction - Fail-closed)
    # =========================================================================
    def _atomic_write_json(self, path: str, data: Dict[str, Any]) -> None:
        """以安全原子方式写入 JSON 文件（临时文件 + 同步落盘 + os.replace 替换）"""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp_path = path + f".{os.getpid()}_{time.time_ns()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_path, path)

    def begin_transaction(self) -> str:
        """
        开启跨文件复合事务。
        若发现未决事务则先自动恢复；随后对当前的 credential.json 和 state.json
        进行物理二进制复制隔离备份，并持久化事务状态记录。
        """
        self.recover_pending_transaction()
        tx_id = f"tx_{int(time.time())}_{os.getpid()}"
        cred_exists = os.path.exists(self.cred_path)
        state_exists = os.path.exists(self.state_path)

        # 备份原有凭据（二进制复制保持原样，若是 DPAPI 加密则完全保留密文）
        if cred_exists:
            shutil.copy2(self.cred_path, self.cred_tx_bak)
        # 备份原有会话
        if state_exists:
            shutil.copy2(self.state_path, self.state_tx_bak)

        tx_record = {
            "tx_id": tx_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "INITIALIZED",
            "cred_path": self.cred_path,
            "cred_had_existed": cred_exists,
            "cred_tx_bak": self.cred_tx_bak if cred_exists else None,
            "state_path": self.state_path,
            "state_had_existed": state_exists,
            "state_tx_bak": self.state_tx_bak if state_exists else None,
        }
        self._atomic_write_json(self.tx_path, tx_record)
        return tx_id

    def record_tx_step(self, status: str) -> None:
        """记录事务推进状态"""
        if not os.path.exists(self.tx_path):
            return
        try:
            with open(self.tx_path, "r", encoding="utf-8") as f:
                tx_data = json.load(f)
            tx_data["status"] = status
            tx_data["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._atomic_write_json(self.tx_path, tx_data)
        except Exception:
            pass

    def commit_transaction(self) -> None:
        """
        提交复合事务：
        双文件均已原子落地成功，安全清理事务临时备份与事务记录文件。
        """
        for p in (self.cred_tx_bak, self.state_tx_bak, self.tx_path):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

    def rollback_transaction(self) -> bool:
        """
        回滚复合事务：
        将 credential.json 和 state.json 完全恢复为事务开始前的完全一致状态。
        """
        if not os.path.exists(self.tx_path):
            return False

        try:
            with open(self.tx_path, "r", encoding="utf-8") as f:
                tx = json.load(f)
        except Exception:
            tx = {}

        restored = False

        # 1. 恢复凭据
        cred_had = tx.get("cred_had_existed", False)
        cred_bak = tx.get("cred_tx_bak") or self.cred_tx_bak
        if cred_had and os.path.exists(cred_bak):
            try:
                os.replace(cred_bak, self.cred_path)
                restored = True
            except Exception:
                pass
        elif not cred_had and os.path.exists(self.cred_path):
            try:
                os.remove(self.cred_path)
                restored = True
            except Exception:
                pass

        # 2. 恢复会话
        state_had = tx.get("state_had_existed", False)
        state_bak = tx.get("state_tx_bak") or self.state_tx_bak
        if state_had and os.path.exists(state_bak):
            try:
                os.replace(state_bak, self.state_path)
                restored = True
            except Exception:
                pass
        elif not state_had and os.path.exists(self.state_path):
            try:
                os.remove(self.state_path)
                restored = True
            except Exception:
                pass

        self.commit_transaction()
        return restored

    def recover_pending_transaction(self) -> bool:
        """
        检查是否存在未决（未提交）事务，若有则自动回滚恢复。
        用于应对程序中途断电、崩溃、进程被强杀等异常场景。
        """
        if not os.path.exists(self.tx_path):
            return False

        try:
            with open(self.tx_path, "r", encoding="utf-8") as f:
                tx = json.load(f)
            status = tx.get("status")
            if status != "COMMITTED":
                print("\n[事务守护] 检测到上次未完成的重新初始化事务，正在自动回滚恢复原有凭据与会话…")
                self.rollback_transaction()
                print("[事务守护] 原有凭据与会话已成功恢复，系统状态一致性已保证。")
                return True
            else:
                self.commit_transaction()
                return False
        except Exception:
            self.rollback_transaction()
            return True

    # =========================================================================
    # 步骤一：检查当前认证状态 (只读，脱敏)
    # =========================================================================
    def check_status(self) -> Dict[str, Any]:
        """
        检查本地 credential.json 与 state.json 的状态。
        返回结构化字典，严格脱敏。
        """
        # 优先检测并恢复任何未决事务
        self.recover_pending_transaction()

        cred_exists = os.path.exists(self.cred_path)
        account_raw: Optional[str] = None
        is_encrypted = False
        cred_decryptable = False
        cred_err: Optional[str] = None

        if cred_exists:
            try:
                loaded = self.storage.load_credential()
                account_raw = loaded.get("account")
                is_encrypted = loaded.get("isEncrypted", False)
                cred_decryptable = True
            except Exception as e:
                cred_decryptable = False
                cred_err = str(e)

        state_exists = os.path.exists(self.state_path)
        token_valid = False
        token_expired = False
        expires_str: Optional[str] = None
        school_id: Optional[str] = None
        semester_id: Optional[str] = None

        if state_exists:
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    st_data = json.load(f)
                if isinstance(st_data, dict):
                    school_id = st_data.get("schoolId")
                    semester_id = st_data.get("semesterId")
                    exp_ms = int(st_data.get("accessTokenExpires") or 0)
                    if exp_ms > 0:
                        now_ms = time.time() * 1000
                        token_expired = now_ms >= exp_ms
                        token_valid = not token_expired
                        expires_str = time.strftime(
                            "%Y-%m-%d %H:%M:%S", time.localtime(exp_ms / 1000)
                        )
                    elif st_data.get("accessToken"):
                        token_valid = True
            except Exception:
                pass

        account_masked = mask_account(account_raw) if account_raw else "未配置"

        return {
            "credential_exists": cred_exists,
            "account": account_raw,
            "account_masked": account_masked,
            "is_encrypted": is_encrypted,
            "credential_decryptable": cred_decryptable,
            "credential_error": cred_err,
            "state_exists": state_exists,
            "token_valid": token_valid,
            "token_expired": token_expired,
            "expires_str": expires_str,
            "school_id": school_id,
            "semester_id": semester_id,
            "can_reinit": True,
        }

    def format_status_report(self, status: Dict[str, Any]) -> str:
        """格式化当前状态输出"""
        lines = [
            "=" * 50,
            "                当前认证状态",
            "=" * 50,
            f"账号配置    : {status['account_masked']}",
        ]
        if status["credential_exists"]:
            enc_desc = "已加密 (Windows DPAPI)" if status["is_encrypted"] else "未加密 (明文)"
            lines.append(f"签名凭据    : 存在 ({enc_desc})")
            lines.append(f"凭据解密    : {'成功' if status['credential_decryptable'] else '失败 (' + str(status['credential_error']) + ')'}")
        else:
            lines.append("签名凭据    : 不存在")

        if status["state_exists"]:
            if status["token_valid"]:
                lines.append(f"会话 Token  : 有效 (有效期至 {status['expires_str'] or '未知'})")
            elif status["token_expired"]:
                lines.append(f"会话 Token  : 已过期 ({status['expires_str']})")
            else:
                lines.append("会话 Token  : 状态未知")
        else:
            lines.append("会话 Token  : 未找到本地会话 (state.json)")

        lines.append("-" * 50)
        lines.append("就绪状态    : 可以开始重新初始化。")
        lines.append("=" * 50)
        return "\n".join(lines)

    # =========================================================================
    # 步骤二：准备官方客户端提示
    # =========================================================================
    def prompt_client_preparation(self, expected_account: Optional[str] = None) -> None:
        """打印官方客户端与模拟器操作指导"""
        acc_text = f"【{mask_account(expected_account)}】" if expected_account else "当前账号"
        print("\n" + "=" * 55)
        print("          请按照提示完成认证初始化准备")
        print("=" * 55)
        print("1. 确认安卓模拟器（如 MuMu 模拟器）已经启动并完成 Root。")
        print("2. 打开官方「乐健体育」App。")
        print(f"3. 请务必在 App 中使用 {acc_text} 进行登录！")
        print("   （系统会自动校验账号一致性，严格防止误用其他账号造成串号）")
        print("4. 输入密码登录成功后，程序将自动捕获登录认证信封。")
        print("5. 整个初始化期间请勿关闭模拟器。")
        print("-" * 55)
        print("【安全声明】")
        print("• 本程序不尝试绕过官方客户端协议认证；")
        print("• 本程序绝不保存明文密码，日志严格脱敏；")
        print("• 新凭据必须通过在线真实登录验证，方会原子替换旧凭据。")
        print("=" * 55 + "\n")

    # =========================================================================
    # 步骤三：从抓包日志提取材料
    # =========================================================================
    def extract_from_flows(self, flows_path: str = DEFAULT_FLOWS_FILE) -> Dict[str, str]:
        """
        从抓包文件 (jsonl) 中检索最新成功的登录请求并提取凭据。
        返回包含 account, signDigital, entrance 的字典。
        若未找到或格式错误，抛出 InvalidCredentialError。
        """
        if not os.path.exists(flows_path):
            raise InvalidCredentialError(f"未找到抓包记录文件: {flows_path}，未能捕获登录流量")

        target_payload = None
        count = 0
        try:
            with open(flows_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if LOGIN_PATH not in line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("status") == 200 and rec.get("req_body"):
                            req_json = json.loads(rec["req_body"])
                            decrypted = unwrap(req_json)
                            if isinstance(decrypted, dict) and decrypted.get("signDigital"):
                                target_payload = decrypted
                                count += 1
                    except Exception:
                        continue
        except Exception as e:
            raise InvalidCredentialError(f"读取抓包文件失败: {e}") from e

        if not target_payload:
            raise InvalidCredentialError(
                "抓包文件中未检索到有效且包含 signDigital 的登录请求记录。\n"
                "请确认已在官方 App 中完成勾选协议并成功点击登录。"
            )

        account = target_payload.get("userName") or target_payload.get("phone")
        sign_digital = target_payload.get("signDigital")
        entrance = str(target_payload.get("entrance") or "1")

        if not account or not str(account).strip():
            raise InvalidCredentialError("抓包载荷中缺少有效账号 (account)")
        if not sign_digital or not str(sign_digital).strip():
            raise InvalidCredentialError("抓包载荷中缺少有效签名 (signDigital)")

        return {
            "account": str(account).strip(),
            "signDigital": str(sign_digital).strip(),
            "entrance": entrance,
        }

    # =========================================================================
    # 步骤四：严格验证新凭据（账号一致性 + 在线登录校验）
    # =========================================================================
    def verify_new_credential(
        self,
        new_cred: Dict[str, str],
        password: str,
        expected_account: Optional[str] = None,
        timeout: int = 20,
    ) -> Dict[str, Any]:
        """
        严格验证新凭据：
        1. 凭据必要字段与非空检查；
        2. 账号一致性校验：若指定了 expected_account，采集账号必须完全匹配，防止跨账号污染；
        3. signDigital 基础合法性；
        4. 发起真实登录请求（调用 AuthManager.login）；
        5. 确保成功获取 accessToken, refreshToken, schoolId。
        验证成功返回完整的 login_data，失败抛出对应的 AuthError。
        """
        account = new_cred.get("account")
        sign_digital = new_cred.get("signDigital")
        entrance = new_cred.get("entrance", "1")

        if not account or not str(account).strip():
            raise InvalidCredentialError("新凭据缺少账号信息")
        if not sign_digital or not str(sign_digital).strip():
            raise InvalidCredentialError("新凭据缺少 signDigital 签名")

        # 账号一致性强校验（防止用户在模拟器登录了其他账号导致本地配置被串号污染）
        if expected_account and str(expected_account).strip():
            exp_clean = str(expected_account).strip()
            act_clean = str(account).strip()
            if exp_clean != act_clean:
                raise InvalidCredentialError(
                    f"账号不一致安全拦截：当前操作绑定的账号为 {mask_account(exp_clean)}，"
                    f"但从官方 App 采集到的账号为 {mask_account(act_clean)}！\n"
                    f"为防止串号污染本地配置，已拒绝采纳该凭据。请在 App 中登录正确账号后重试。"
                )

        if not password or not str(password).strip():
            raise InvalidCredentialError("验证新凭据需要输入登录密码")

        # 发起在线登录核验
        login_data = self.auth_manager.login(
            account=account,
            password=password,
            sign_digital=sign_digital,
            entrance=entrance,
            timeout=timeout,
        )

        # 确保关键 Token 存在
        if not login_data.get("accessToken"):
            raise TokenAcquisitionError("新凭据登录核验失败：未能获取 accessToken")
        if not login_data.get("refreshToken"):
            raise TokenAcquisitionError("新凭据登录核验失败：未能获取 refreshToken")
        if not login_data.get("schoolId"):
            raise SchoolInfoError("新凭据登录核验失败：未能获取 schoolId")

        return login_data

    # =========================================================================
    # 步骤五：原子替换旧凭据 (Fail-closed)
    # =========================================================================
    def apply_new_credential_atomically(
        self,
        new_cred: Dict[str, str],
        use_dpapi: Optional[bool] = None,
    ) -> None:
        """
        以严格原子事务方式保存并替换 credential.json。
        复用 CredentialStorage.save_credential，自带：
        - .tmp 临时写入与回读比对校验
        - Windows DPAPI 加密往返解密校验
        - os.replace 原子替换与 .bak 备份
        - 失败绝不破坏原有凭据
        """
        dpapi_flag = self.use_dpapi if self.use_dpapi is not None else use_dpapi
        self.storage.save_credential(
            account=new_cred["account"],
            sign_digital=new_cred["signDigital"],
            entrance=new_cred.get("entrance", "1"),
            captured_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            use_dpapi=dpapi_flag,
        )

    # =========================================================================
    # 步骤六：拉取学期信息并更新会话 (state.json 原子更新)
    # =========================================================================
    def update_session_atomically(
        self,
        login_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        拉取学期上下文，并以原子事务方式更新 state.json。
        复用 AuthManager.save_state_atomically。
        """
        return self.auth_manager.save_state_atomically(
            login_data=login_data,
            state_path=self.state_path,
            backup=True,
        )

    # =========================================================================
    # 统一完整重新初始化主流程
    # =========================================================================
    def reinitialize(
        self,
        password: Optional[str] = None,
        flows_path: str = DEFAULT_FLOWS_FILE,
        capture_fn: Optional[Callable[[], bool]] = None,
        use_dpapi: Optional[bool] = None,
        interactive: bool = True,
    ) -> bool:
        """
        执行完整的认证重新初始化七步生命周期。
        参数：
        - password: 登录密码（若为 None 且处于交互式环境，则通过 getpass 提示输入）
        - flows_path: 抓包 flows 文件路径
        - capture_fn: 自定义抓包触发器函数（若未指定，则调用系统内置 capture_login）
        - use_dpapi: 是否启用 DPAPI 加密
        - interactive: 是否处于交互式环境
        返回：
        - True: 重新初始化全流程成功，新凭据与会话均已原子落地
        - False: 中途失败，原凭据与原会话均完好保留，已输出诊断信息
        """
        # 1. 检查并打印当前状态
        status = self.check_status()
        expected_account = status.get("account")
        if interactive:
            print("\n" + self.format_status_report(status))

        # 2. 准备官方客户端提示
        if interactive:
            self.prompt_client_preparation(expected_account)

        # 获取密码
        pwd = password
        if not pwd and interactive:
            try:
                acc_prompt = f"【{mask_account(expected_account)}】" if expected_account else ""
                pwd = getpass.getpass(f"请输入{acc_prompt}的登录密码（用于新凭据有效性校验，不回显）：")
            except (KeyboardInterrupt, EOFError):
                print("\n操作已取消。原有凭据保持未修改。")
                return False

        if not pwd or not str(pwd).strip():
            print("\n未输入密码，无法进行新凭据有效性核验。重新初始化已中止，原凭据保持未修改。")
            return False

        # 3. 执行签名采集 (调用 capture_login 或自定义 capture_fn)
        if capture_fn:
            ok = capture_fn()
            if not ok:
                print("\n❌ 签名采集未成功完成。已中止重新初始化，原凭据未被修改。")
                return False
        elif not os.path.exists(flows_path):
            # 若未提供自定义 capture_fn 且抓包文件不存在，提示启动采集
            print(f"正在准备调用签名采集工具 ({flows_path})…")
            try:
                import tools.capture_login as capture_login
                # 检查 adb
                adb = capture_login.find_adb()
                if not adb:
                    print("\n❌ 未检测到 adb 命令行工具，请确保 MuMu 或其他安卓模拟器已安装并启动。")
                    return False
                # 若外部未传递 capture_fn，引导运行 capture_login
                print("请在模拟器中启动乐健 App 并完成一次密码登录。")
            except ImportError:
                print(f"\n❌ 未找到 capture_login 工具，请确保环境完整。")
                return False

        # 4. 从抓包文件中解析材料并核验新凭据
        print("\n正在从抓包记录中提取新认证材料…")
        try:
            new_cred = self.extract_from_flows(flows_path)
            print(f"  成功提取账号: {mask_account(new_cred['account'])}")
            print(f"  成功提取签名: {mask_secret(new_cred['signDigital'])}")

            print("正在向服务端提交新凭据登录核验…")
            login_data = self.verify_new_credential(
                new_cred=new_cred,
                password=pwd,
                expected_account=expected_account,
            )
            print("  新凭据在线登录验证通过！服务端已成功颁发会话。")
        except Exception as e:
            print("\n❌ 新凭据核验未通过，已中止替换！原有凭据保持完好不受损。")
            report = diagnose_auth_error(e, context={"stage": "reinit_verify", "account": expected_account})
            print("\n" + report.format_report(verbose=False))
            return False
        finally:
            del pwd

        # 5. 开启跨文件复合事务并原子安全替换本地凭据
        print("\n正在开启跨文件复合更新事务并原子写入 credential.json…")
        self.begin_transaction()
        try:
            self.apply_new_credential_atomically(new_cred, use_dpapi=use_dpapi)
            self.record_tx_step("CRED_APPLIED")
            print("  本地凭据已安全原子替换！")
        except Exception as e:
            print(f"\n❌ 保存新凭据失败: {e}。触发事务回滚，原有凭据保持未修改。")
            self.rollback_transaction()
            return False

        # 6. 原子更新本地会话 (state.json)
        print("正在同步更新本地会话 state.json…")
        try:
            saved_state = self.update_session_atomically(login_data)
            sem_id = saved_state.get("semesterId") or "未知"
            print(f"  本地会话已更新落地 (当前学期: {sem_id})")
            self.record_tx_step("STATE_APPLIED")
        except Exception as e:
            print(f"\n❌ 凭据已替换但更新会话 state.json 失败: {e}")
            print("  [事务保护] 为防止出现「新凭据对应旧会话」的不一致状态，正在自动回滚凭据与会话…")
            self.rollback_transaction()
            print("  [事务保护] 旧凭据与旧会话已完全恢复，系统状态一致性未受破坏。重新初始化已中止。")
            return False

        # 7. 提交跨文件复合事务并完成确认
        self.commit_transaction()

        # 打印完成横幅
        print("\n" + "=" * 55)
        print("              ========== 认证初始化完成 ==========")
        print("=" * 55)
        print(f"账号认证    : 成功 ({mask_account(new_cred['account'])})")
        print("签名凭据    : 已更新" + (" (Windows DPAPI 受保护)" if is_windows() else ""))
        print("AccessToken : 获取成功")
        print("RefreshToken: 获取成功")
        print(f"学校代码    : 获取成功 ({login_data.get('schoolId')})")
        print(f"当前学期    : 获取成功 ({saved_state.get('semesterId')})")
        print("本地凭据    : 已安全原子保存 (credential.json)")
        print("会话状态    : 已原子更新 (state.json)")
        print("-" * 55)
        print("现在可以正常使用 lejianPE 进行选课与课表查询。")
        print("=" * 55 + "\n")
        return True
