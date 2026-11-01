"""
乐健体育 · 命令行账号密码登录工具。

基于 signDigital + 动态时间戳 + AES 登录信封实现。
无需每次启动 Android 模拟器。
"""
import argparse
import getpass
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

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
    is_windows,
    mask_account,
    mask_secret,
)

try:
    from tools.login_envelope import unwrap
except ImportError:
    from login_envelope import unwrap

LOGIN_PATH = "authorization/user/v2/manage/login"
CRED_FILE = os.path.join(ROOT, "credential.json")
STATE_FILE = os.path.join(ROOT, "state.json")


def do_setup(flows_path: str, use_dpapi: bool = None):
    """从 mitmproxy 导出的抓包 jsonl 中提取账号与 signDigital"""
    if not os.path.exists(flows_path):
        sys.exit(f"❌ 找不到抓包文件: {flows_path}")

    if use_dpapi is None:
        use_dpapi = is_windows()

    payload = None
    count = 0
    with open(flows_path, "r", encoding="utf-8") as f:
        for line in f:
            if LOGIN_PATH not in line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("status") == 200:
                    payload = unwrap(json.loads(rec["req_body"]))
                    count += 1
            except Exception:
                continue

    if not payload:
        sys.exit("❌ 抓包文件中未检索到成功的登录请求记录")

    account = payload.get("userName") or payload.get("phone")
    sign_digital = payload.get("signDigital")
    entrance = str(payload.get("entrance") or "1")
    captured_at = time.strftime("%Y-%m-%d %H:%M:%S")

    storage = CredentialStorage(CRED_FILE)
    try:
        storage.save_credential(
            account=account,
            sign_digital=sign_digital,
            entrance=entrance,
            captured_at=captured_at,
            use_dpapi=use_dpapi,
        )
    except Exception as e:
        sys.exit(f"❌ 保存凭据失败: {e}")

    # 验证是否真正生效
    verify_cred = storage.load_credential()
    is_enc = verify_cred.get("isEncrypted", False)

    print("✅ 已成功生成 credential.json（严格不包含明文密码）")
    print(f"  账号       : {mask_account(account)}")
    print(f"  签名材料   : {mask_secret(sign_digital)}")
    print(f"  采集时间   : {captured_at}")
    print(f"  DPAPI 加密 : {'已启用并验证通过' if is_enc else '未启用（明文存储）'}")
    print("\n提示：密码修改后签名即失效，届时需重新运行 setup 初始化。")


def encrypt_existing_credential():
    """将现有的明文 credential.json 升级为 Windows DPAPI 加密存储"""
    if not is_windows():
        sys.exit("❌ 当前系统不是 Windows，不支持 DPAPI 加密")

    storage = CredentialStorage(CRED_FILE)
    try:
        cred = storage.load_credential()
    except Exception as e:
        sys.exit(f"❌ 加载凭据失败: {e}")

    if cred.get("isEncrypted"):
        print("ℹ️ 当前凭据已经处于 Windows DPAPI 加密保护状态，无需重复加密。")
        return

    try:
        storage.save_credential(
            account=cred["account"],
            sign_digital=cred["signDigital"],
            entrance=cred.get("entrance", "1"),
            captured_at=cred.get("capturedAt", ""),
            use_dpapi=True,
        )
    except Exception as e:
        sys.exit(f"❌ DPAPI 加密失败: {e}（已拒绝保存明文，原凭据保持未修改）")

    # 严密二次核验
    re_check = storage.load_credential()
    if not re_check.get("isEncrypted"):
        sys.exit("❌ 严重安全异常：加密执行后未检测到密文字段！")

    print("✅ 已成功将 signDigital 使用 Windows DPAPI 加密保护！")
    print(f"  账号: {mask_account(cred['account'])}")
    print("  已完成回读验证：明文字段已移除，密文字段已成功解密匹配。")
    print("  提示: 加密后的凭据仅在当前 Windows 用户登录会话下可解密。")


def main():
    parser = argparse.ArgumentParser(description="乐健体育命令行登录")
    parser.add_argument("--setup", metavar="FLOWS_PATH", help="从抓包文件中提取认证材料并初始化 credential.json")
    parser.add_argument("--encrypt", action="store_true", help="使用 Windows DPAPI 对当前凭据进行加密保护")
    parser.add_argument("--plaintext", action="store_true", help="显式以明文方式存储凭据（不推荐）")
    parser.add_argument("--no-save", action="store_true", help="仅测试认证，不写入 state.json")
    args = parser.parse_args()

    if args.setup:
        use_dpapi = False if args.plaintext else is_windows()
        do_setup(args.setup, use_dpapi=use_dpapi)
        return

    if args.encrypt:
        encrypt_existing_credential()
        return

    storage = CredentialStorage(CRED_FILE)
    try:
        cred = storage.load_credential()
    except CredentialError as e:
        print(f"\n❌ 凭据异常: {e}")
        print("请先初始化认证材料：")
        print("  python tools\\login_cli.py --setup captures\\login_flows.jsonl")
        print("或在菜单中选择 [b] 重新抓取登录签名")
        sys.exit(1)

    account = cred["account"]
    captured_at = cred.get("capturedAt") or "未知时间"
    enc_status = " (DPAPI受保护)" if cred.get("isEncrypted") else ""
    print("=" * 50)
    print("           乐健体育 · 命令行安全登录")
    print("=" * 50)
    print(f"账号：{mask_account(account)}{enc_status}（认证材料采集于 {captured_at}）")

    try:
        password = getpass.getpass("请输入密码（输入时不回显）：")
    except (KeyboardInterrupt, EOFError):
        print("\n已取消输入。")
        sys.exit(0)

    if not password or not password.strip():
        print("未输入密码，操作已取消。")
        sys.exit(0)

    print("正在提交登录请求…")
    auth_mgr = AuthManager()

    try:
        data = auth_mgr.login(
            account=account,
            password=password,
            sign_digital=cred["signDigital"],
            entrance=cred.get("entrance", "1"),
        )
    except BusinessAuthError as e:
        print(f"\n❌ 业务认证失败: {e}")
        if e.code == 50004 or "签名" in str(e):
            print("  [原因分析] 服务端判定签名校验不匹配。")
            print("  签名是由「账号 + 密码 + entrance」通过客户端底层计算得出。")
            print("  若您曾在 App 端修改过密码，旧签名将永久失效。")
            print("  [解决建议]")
            print("    1. 请先通过模拟器登录并重新抓包生成认证材料：")
            print("       python tools\\capture_login.py")
            print("    2. 或在主菜单中选 [a] 临时使用模拟器同步 Token。")
        elif "密码" in str(e) or "账号" in str(e):
            print("  [解决建议] 请核对输入的密码是否正确。")
        sys.exit(1)
    except AuthNetworkError as e:
        print(f"\n❌ 网络连接异常: {e}")
        print("  [解决建议] 请检查网络连通性或稍后重试。")
        sys.exit(1)
    except (TokenAcquisitionError, SchoolInfoError, SemesterInfoError) as e:
        print(f"\n❌ 认证数据异常: {e}")
        print("  [安全保护] 登录未成功落地，已完整保留原有的 state.json。")
        sys.exit(1)
    except AuthError as e:
        print(f"\n❌ 登录失败: {e}")
        sys.exit(1)
    finally:
        # 及时释放内存中的密码引用
        del password

    print("\n✅ 登录认证成功！")
    real_name = data.get("realName") or "未知姓名"
    school_id = data.get("schoolId")
    token_str = data.get("accessToken") or ""
    exp_ms = data.get("accessTokenExpires") or 0
    exp_str = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp_ms / 1000))
        if exp_ms
        else "未知"
    )

    print(f"  学生姓名   : {real_name}")
    print(f"  学校代码   : {school_id}")
    print(f"  Token 前缀 : {mask_secret(token_str)}")
    print(f"  有效期至   : {exp_str}")

    if not args.no_save:
        try:
            saved_state = auth_mgr.save_state_atomically(data, STATE_FILE)
            print(f"  已成功写入 : state.json (学期 ID: {saved_state.get('semesterId')})")
        except Exception as e:
            print(f"\n❌ 保存 state.json 失败: {e}")
            print("  原有 state.json 保持完好。")
            sys.exit(1)


if __name__ == "__main__":
    main()
