"""
凭据安全存储与管理模块。

支持：
1. 本地 credential.json 校验与安全读取。
2. Windows DPAPI (Data Protection API) 对敏感认证材料 (signDigital) 的加解密保护。
3. Fail-Closed 安全屏障：DPAPI 加密失败时绝对不回退到明文保存，强行拒绝并保护原有凭据。
4. 严格日志脱敏工具（账号掩码、密钥掩码、Token 掩码）。
5. 事务性原子写入与回滚保护。
"""
import base64
import json
import os
import shutil
import sys
from typing import Dict, Optional

from core.auth_error import CredentialError, InvalidCredentialError


def is_windows() -> bool:
    return sys.platform.startswith("win")


def dpapi_protect(plaintext: bytes, description: str = "lejian_credential") -> bytes:
    """使用 Windows DPAPI 加密数据，仅当前 Windows 用户能够解密"""
    if not is_windows():
        raise NotImplementedError("DPAPI 仅在 Windows 系统可用")
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    blob_in = DATA_BLOB(
        len(plaintext),
        ctypes.cast(ctypes.create_string_buffer(plaintext), ctypes.POINTER(ctypes.c_byte)),
    )
    blob_out = DATA_BLOB()
    ret = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        description,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ret:
        raise OSError("DPAPI CryptProtectData 加密失败")
    cipher = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return cipher


def dpapi_unprotect(ciphertext: bytes) -> bytes:
    """使用 Windows DPAPI 解密数据"""
    if not is_windows():
        raise NotImplementedError("DPAPI 仅在 Windows 系统可用")
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", ctypes.wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    blob_in = DATA_BLOB(
        len(ciphertext),
        ctypes.cast(ctypes.create_string_buffer(ciphertext), ctypes.POINTER(ctypes.c_byte)),
    )
    blob_out = DATA_BLOB()
    ret = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    )
    if not ret:
        raise OSError("DPAPI CryptUnprotectData 解密失败（当前用户无权访问或数据损坏）")
    plain = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return plain


def mask_account(acc: Optional[str]) -> str:
    """脱敏手机号或账号：保留前 3 位和后 4 位"""
    s = str(acc or "").strip()
    if len(s) >= 7:
        return f"{s[:3]}****{s[-4:]}"
    elif len(s) >= 4:
        return f"{s[:2]}**{s[-1:]}"
    return "***"


def mask_secret(sec: Optional[str]) -> str:
    """脱敏敏感字符串（Token / signDigital）：保留前 4 位和后 4 位及总长度"""
    s = str(sec or "").strip()
    if len(s) >= 12:
        return f"{s[:4]}...{s[-4:]} (长度={len(s)})"
    elif len(s) > 0:
        return f"*** (长度={len(s)})"
    return "None"


class CredentialStorage:
    """
    负责凭据的持久化加载、校验与安全存储。
    """

    def __init__(self, cred_path: str):
        self.cred_path = os.path.abspath(cred_path)

    def load_credential(self) -> Dict[str, str]:
        """
        加载并解密凭据。
        必须包含:
          - account: 账号
          - signDigital: 签名（明文或经 DPAPI 解密后的明文）
          - entrance: 入口标识（默认 "1"）
        """
        if not os.path.exists(self.cred_path):
            raise CredentialError(f"未找到凭据文件: {self.cred_path}。请先运行登录初始化。")

        try:
            with open(self.cred_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise InvalidCredentialError(f"凭据文件损坏或不是有效 JSON: {e}") from e
        except Exception as e:
            raise CredentialError(f"读取凭据文件失败: {e}") from e

        if not isinstance(data, dict):
            raise InvalidCredentialError("凭据格式错误：根节点必须为 JSON 对象")

        account = data.get("account") or data.get("userName") or data.get("phone")
        if not account or not str(account).strip():
            raise InvalidCredentialError("凭据缺少有效账号 (account)")

        sign_digital = data.get("signDigital")
        encrypted_sign = data.get("signDigitalEncrypted")

        if encrypted_sign:
            try:
                raw_cipher = base64.b64decode(encrypted_sign)
                sign_digital = dpapi_unprotect(raw_cipher).decode("utf-8")
            except Exception as e:
                raise CredentialError(f"解密凭据 signDigital 失败: {e}") from e

        if not sign_digital or not str(sign_digital).strip():
            raise InvalidCredentialError("凭据缺少认证签名 (signDigital)，请重新采集或初始化")

        entrance = str(data.get("entrance") or "1")
        captured_at = data.get("capturedAt") or ""

        return {
            "account": str(account).strip(),
            "signDigital": str(sign_digital).strip(),
            "entrance": entrance,
            "capturedAt": captured_at,
            "isEncrypted": bool(encrypted_sign),
        }

    def save_credential(
        self,
        account: str,
        sign_digital: str,
        entrance: str = "1",
        captured_at: str = "",
        use_dpapi: Optional[bool] = None,
    ) -> None:
        """
        原子化保存凭据到磁盘。
        安全原则：
        1. Windows 默认采用 DPAPI 加密存储 (use_dpapi=True)。
        2. 若启用 DPAPI，加密失败时绝对不降级为明文，抛出 CredentialError 并保持原文件完整。
        3. 写入前使用临时文件，并校验落盘内容（确认明文不泄露、密文解密一致）后原子替换。
        """
        if not account or not str(account).strip():
            raise InvalidCredentialError("保存凭据失败：账号不能为空")
        if not sign_digital or not str(sign_digital).strip():
            raise InvalidCredentialError("保存凭据失败：signDigital 不能为空")

        clean_account = str(account).strip()
        clean_sign = str(sign_digital).strip()

        # 默认策略：Windows 环境下默认开启 DPAPI 加密
        if use_dpapi is None:
            use_dpapi = is_windows()

        payload: Dict[str, str] = {
            "account": clean_account,
            "entrance": str(entrance or "1").strip(),
            "capturedAt": captured_at or "",
        }

        if use_dpapi:
            if not is_windows():
                raise CredentialError("当前系统非 Windows，无法使用 DPAPI 安全加密存储")
            try:
                cipher = dpapi_protect(clean_sign.encode("utf-8"))
                payload["signDigitalEncrypted"] = base64.b64encode(cipher).decode("ascii")
            except Exception as e:
                # 严格禁止回退到明文（Fail-closed 安全屏障）
                raise CredentialError(f"DPAPI 加密失败: {e}。为防止凭据泄露，已拒绝保存！原有凭据未被修改。") from e
        else:
            # 明确非加密保存（仅在非 Windows 或显式指定时）
            payload["signDigital"] = clean_sign

        os.makedirs(os.path.dirname(self.cred_path), exist_ok=True)
        tmp_path = self.cred_path + ".tmp"
        bak_path = self.cred_path + ".bak"

        # 若已有凭据，先备份
        if os.path.exists(self.cred_path):
            try:
                shutil.copy2(self.cred_path, bak_path)
            except Exception:
                pass

        try:
            # 1. 写入临时文件
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)

            # 2. 严格回读验证
            with open(tmp_path, "r", encoding="utf-8") as f:
                verify_data = json.load(f)

            if use_dpapi:
                if "signDigital" in verify_data:
                    raise CredentialError("安全校验失败：加密凭据中意外包含明文 signDigital")
                if not verify_data.get("signDigitalEncrypted"):
                    raise CredentialError("安全校验失败：未检测到 signDigitalEncrypted 密文字段")
                # 验证解密一致性
                test_cipher = base64.b64decode(verify_data["signDigitalEncrypted"])
                decrypted = dpapi_unprotect(test_cipher).decode("utf-8")
                if decrypted != clean_sign:
                    raise CredentialError("安全校验失败：DPAPI 往返解密结果与原始签名不一致")
            else:
                if verify_data.get("signDigital") != clean_sign:
                    raise CredentialError("写入校验失败：保存的 signDigital 不匹配")

            # 3. 校验完全通过，原子替换
            os.replace(tmp_path, self.cred_path)
        except Exception as e:
            # 异常时清理临时文件，确保旧文件完好
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if isinstance(e, CredentialError):
                raise
            raise CredentialError(f"原子化保存凭据失败: {e}") from e
