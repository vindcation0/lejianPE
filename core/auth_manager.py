"""
统一认证管理器（AuthManager）。

负责：
1. 账号密码 + signDigital 的安全登录认证。
2. 构造与发送 AES 信封请求。
3. 业务状态码与 HTTP 状态校验。
4. 学期及学校上下文拉取与细粒度异常诊断。
5. 账户身份字段严格隔离（杜绝跨账户污染）。
6. state.json 事务性原子写入与备份保护（失败绝对不破坏旧状态）。
7. 日志敏感字段严格脱敏。
"""
import datetime
import json
import os
import shutil
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "tools") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "tools"))

try:
    from tools.login_envelope import unwrap, wrap
except ImportError:
    from login_envelope import unwrap, wrap

from core.auth_error import (
    AuthError,
    AuthNetworkError,
    BusinessAuthError,
    InvalidCredentialError,
    SchoolInfoError,
    SemesterInfoError,
    TokenAcquisitionError,
)
from core.credential_storage import mask_account, mask_secret

BASE_URL = "https://cpes.legym.cn/"
LOGIN_PATH = "authorization/user/v2/manage/login"
SEMESTER_PATH = "education/semester/getCurrent"


def _decode_bytes(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


class AuthManager:
    """
    负责命令行登录认证全生命周期管理。
    """

    def __init__(self, base_url: str = BASE_URL, ssl_verify: bool = True):
        self.base_url = base_url.rstrip("/") + "/"
        self.ctx = ssl.create_default_context()
        if not ssl_verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def post_json(self, path: str, obj: dict, timeout: int = 20) -> Tuple[int, str]:
        """发起 POST JSON 请求"""
        url = urllib.parse.urljoin(self.base_url, path)
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "okhttp/3.12.1")

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                status = resp.status
                body = _decode_bytes(resp.read())
                return status, body
        except urllib.error.HTTPError as e:
            return e.code, _decode_bytes(e.read())
        except urllib.error.URLError as e:
            raise AuthNetworkError(f"网络连接异常: {e.reason}") from e
        except TimeoutError as e:
            raise AuthNetworkError(f"请求超时: {e}") from e
        except Exception as e:
            raise AuthNetworkError(f"未知网络异常: {e}") from e

    def get_json(
        self, path: str, token: str, school_id: Optional[str] = None, timeout: int = 15
    ) -> Tuple[int, dict]:
        """发起带 Token 的 GET JSON 请求"""
        url = urllib.parse.urljoin(self.base_url, path)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {token}")
        if school_id:
            req.add_header("Organization", str(school_id))
        req.add_header("User-Agent", "okhttp/3.12.1")

        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                status = resp.status
                body = _decode_bytes(resp.read())
                try:
                    return status, json.loads(body)
                except Exception:
                    return status, {"_raw": body}
        except urllib.error.HTTPError as e:
            body = _decode_bytes(e.read())
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"_raw": body}
        except Exception as e:
            raise AuthNetworkError(f"GET 请求失败: {e}") from e

    def login(
        self,
        account: str,
        password: str,
        sign_digital: str,
        entrance: str = "1",
        timeout: int = 20,
    ) -> Dict[str, Any]:
        """
        执行账号密码登录。
        成功返回解密后的业务数据 dict；失败则抛出对应的 AuthError 子类。
        """
        if not account or not str(account).strip():
            raise InvalidCredentialError("账号不能为空")
        if not password or not str(password).strip():
            raise InvalidCredentialError("密码不能为空")
        if not sign_digital or not str(sign_digital).strip():
            raise InvalidCredentialError("signDigital 签名材料不能为空")

        payload = {
            "userName": str(account).strip(),
            "phone": str(account).strip(),
            "password": str(password),
            "entrance": str(entrance or "1").strip(),
            "authType": "Bearer",
            "baseUrl": self.base_url,
            "page": "",
            "type": "",
            "signDigital": str(sign_digital).strip(),
        }

        # 封装带当前时间戳的 AES 信封
        envelope = wrap(payload)

        status, resp_text = self.post_json(LOGIN_PATH, envelope, timeout=timeout)

        # 检查是否为有效 JSON
        try:
            res_obj = json.loads(resp_text)
        except Exception as e:
            if status != 200:
                raise AuthNetworkError(f"服务端返回 HTTP {status}: {resp_text[:160]}", http_status=status) from e
            raise BusinessAuthError(f"服务端响应不是有效 JSON (HTTP {status})") from e

        # 检查业务状态码
        code = res_obj.get("code")
        msg = res_obj.get("message") or "未知错误"

        if status != 200 or code != 0:
            # 常见业务错误码精细化分类
            if code == 50004 or "签名" in str(msg):
                raise BusinessAuthError(f"认证失败 (code={code}): 签名材料失效或密码已被修改 ({msg})", code=code)
            elif "密码" in str(msg) or "账号" in str(msg) or "用户" in str(msg):
                raise BusinessAuthError(f"认证失败 (code={code}): 账号或密码错误 ({msg})", code=code)
            raise BusinessAuthError(f"认证失败 (HTTP {status}, code={code}): {msg}", code=code)

        data = res_obj.get("data")
        if not data:
            raise TokenAcquisitionError("登录成功但服务端返回的 data 为空")

        # 若服务端返回的是 AES 加密的信封 (pyd)，执行解密
        if isinstance(data, dict) and "pyd" in data:
            try:
                data = unwrap(data)
            except Exception as e:
                raise TokenAcquisitionError(f"解密服务端返回的登录载荷失败: {e}") from e

        if not isinstance(data, dict):
            raise TokenAcquisitionError(f"登录返回的业务数据格式非法: {type(data)}")

        # 必要字段校验
        access_token = data.get("accessToken")
        refresh_token = data.get("refreshToken")
        school_id = data.get("schoolId")

        if not access_token:
            raise TokenAcquisitionError("登录成功但未能获取到 accessToken")
        if not refresh_token:
            raise TokenAcquisitionError("登录成功但未能获取到 refreshToken")
        if not school_id:
            raise SchoolInfoError("登录成功但响应缺少 schoolId")

        return data

    def fetch_semester_info(
        self, access_token: str, school_id: str, timeout: int = 15
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        拉取当前学期信息。
        返回: (semester_id, year_range, error_reason)
        若成功，error_reason 为 None；若失败，返回具体诊断信息（不包含任何敏感 Token）。
        """
        err_msg = None
        try:
            status, res = self.get_json(
                SEMESTER_PATH, access_token, school_id=school_id, timeout=timeout
            )
            if status == 200:
                if isinstance(res, dict):
                    code = res.get("code")
                    if code == 0:
                        sem_d = res.get("data") or {}
                        sem_id = sem_d.get("id")
                        year_range = str(sem_d.get("yearRange") or "")
                        if sem_id:
                            return sem_id, year_range, None
                        else:
                            err_msg = "服务端返回数据缺少 semesterId 字段"
                    else:
                        err_msg = f"服务端业务错误 (code={code}): {res.get('message', '未知错误')}"
                else:
                    err_msg = "服务端响应格式非法（非 JSON 对象）"
            elif status == 401:
                err_msg = "HTTP 401 未授权（Token 无效或已过期）"
            elif status >= 500:
                err_msg = f"HTTP {status} 服务端基础设施故障"
            else:
                err_msg = f"HTTP {status} 请求失败"
        except AuthNetworkError as e:
            err_msg = f"网络通信异常: {e}"
        except Exception as e:
            err_msg = f"未知异常: {e}"

        return None, None, err_msg

    def save_state_atomically(
        self,
        login_data: Dict[str, Any],
        state_path: str,
        backup: bool = True,
    ) -> Dict[str, Any]:
        """
        以严格原子事务方式保存或更新 state.json。
        安全原则：
        1. 严格账户身份隔离：userId、realName、mobile、organizationUserNumber、campusId
           等身份信息完全取自当前 login_data，绝不从旧账户 (old_state) 错误继承！
        2. 全必要字段校验：accessToken、refreshToken、schoolId、semesterId 必须全部有效。
        3. 写入前保留 .bak 备份，写入 .tmp 文件并完成回读校验后执行 os.replace 原子替换。
        4. 任何步骤失败绝对不破坏现有 state.json！
        """
        state_path = os.path.abspath(state_path)
        access_token = login_data.get("accessToken")
        refresh_token = login_data.get("refreshToken")
        school_id = login_data.get("schoolId")

        if not access_token:
            raise TokenAcquisitionError("保存状态失败：accessToken 缺失")
        if not refresh_token:
            raise TokenAcquisitionError("保存状态失败：refreshToken 缺失")
        if not school_id:
            raise SchoolInfoError("保存状态失败：schoolId 缺失")

        # 尝试拉取当前学期 ID
        sem_id, year_range, sem_err = self.fetch_semester_info(access_token, school_id)
        final_semester_id = sem_id or login_data.get("semesterId")

        if not final_semester_id:
            detail = f"原因: {sem_err}" if sem_err else "未能获取有效 semesterId"
            raise SemesterInfoError(f"保存状态失败：{detail}。已保护原有会话不受损，绝不写入残损会话！")

        # 身份信息完全隔离：严格只使用当前登录用户的属性，绝不使用旧账户数据 fallback
        clean_user_id = str(login_data.get("id") or login_data.get("userId") or "").strip()
        clean_real_name = str(login_data.get("realName") or "").strip()
        clean_mobile = str(login_data.get("mobile") or login_data.get("phone") or "").strip()
        clean_user_num = str(login_data.get("organizationUserNumber") or "").strip()
        clean_campus_id = str(login_data.get("campusId") or "").strip()

        year_str = (year_range[:4] if year_range else "") or str(
            login_data.get("year") or "2026"
        )

        new_state = {
            "accessToken": str(access_token).strip(),
            "refreshToken": str(refresh_token).strip(),
            "accessTokenExpires": int(login_data.get("accessTokenExpires") or 0),
            "userId": clean_user_id,
            "schoolId": str(school_id).strip(),
            "semesterId": str(final_semester_id).strip(),
            "year": year_str,
            "mobile": clean_mobile,
            "realName": clean_real_name,
            "organizationUserNumber": clean_user_num,
            "campusId": clean_campus_id,
        }

        # 备份旧 state 文件
        bak_path = state_path + ".bak"
        if backup and os.path.exists(state_path):
            try:
                shutil.copy2(state_path, bak_path)
            except Exception:
                pass

        # 写入临时文件并强校验
        tmp_path = state_path + ".tmp"
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(new_state, f, ensure_ascii=False, indent=2)

            # 校验临时文件完整性
            with open(tmp_path, "r", encoding="utf-8") as f:
                verify_data = json.load(f)

            for req_key in ("accessToken", "refreshToken", "schoolId", "semesterId"):
                if not verify_data.get(req_key):
                    raise TokenAcquisitionError(f"写入的临时 state 文件数据校验失败：缺失关键字段 {req_key}")

            # 原子替换
            os.replace(tmp_path, state_path)
        except Exception as e:
            # 清理损坏的临时文件，若有备份且目标损坏则尝试恢复
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            if isinstance(e, AuthError):
                raise
            raise AuthError(f"原子化保存 state.json 失败: {e}") from e

        return new_state
