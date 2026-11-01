"""
乐健体育登录信封（SecurityAes）编解码。

协议：{"t": <13位毫秒时间戳>, "pyd": "<base64(AES-128-ECB(明文JSON))>"}
密钥：由 t 动态派生 —— shift = abs(t[2:5] - t[4:8]) << t[-1]，拼盐后取前 16 字节。

本模块只做编解码，不含任何网络请求，可离线自测。
"""
import base64
import json
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# 从 libsecurity.so 数据段提取的预置盐
# 乐健 App 逆向硬编码协议常量 (用于 AES 动态密钥派生，非用户私密凭据)
SECRET = "402881ea7c39c5d5017c39d143a8062b"  # gitleaks:allow


def dynamic_key(t) -> bytes:
    """t: 13 位毫秒时间戳（int 或 str）"""
    s = str(int(t))
    if len(s) != 13:
        raise ValueError(f"时间戳必须是 13 位，收到 {len(s)} 位")
    n1 = int(s[2:5])
    n2 = int(s[4:8])
    n3 = int(s[-1])
    shift = abs(n1 - n2) << n3
    return (f"{shift}{SECRET}"[:16]).encode("utf-8")


def _pkcs7_pad(data: bytes, block: int = 16) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


def encrypt_raw(plaintext: bytes, t) -> str:
    key = dynamic_key(t)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    enc = cipher.encryptor()
    ct = enc.update(_pkcs7_pad(plaintext)) + enc.finalize()
    return base64.b64encode(ct).decode()


def decrypt_raw(b64: str, t) -> bytes:
    key = dynamic_key(t)
    ct = base64.b64decode(b64)
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    dec = cipher.decryptor()
    return _pkcs7_unpad(dec.update(ct) + dec.finalize())


def wrap(obj: dict, t=None) -> dict:
    """把明文对象包装成对外信封"""
    t = int(time.time() * 1000) if t is None else int(t)
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return {"t": t, "pyd": encrypt_raw(body, t)}


def unwrap(envelope: dict) -> dict:
    """解开信封，返回明文对象"""
    t = envelope["t"]
    raw = decrypt_raw(envelope["pyd"], t)
    return json.loads(raw.decode("utf-8"))


def _selftest():
    import os
    ok = 0
    for _ in range(200):
        t = int(time.time() * 1000) + os.urandom(1)[0]
        obj = {"userName": "T" * 11, "password": "x", "entrance": None,
               "signDigital": "0" * 40, "n": os.urandom(4).hex()}
        env = wrap(obj, t)
        back = unwrap(env)
        assert back == obj, f"往返失败: {back} != {obj}"
        ok += 1
    print(f"自测通过：{ok} 次随机往返，密钥派生与 AES-ECB 编解码一致")

    t = 1730438000000
    print(f"示例 t={t} -> key={dynamic_key(t)!r}")
    print(f"示例信封: {wrap({'hello': 'world'}, t)}")


if __name__ == "__main__":
    _selftest()
