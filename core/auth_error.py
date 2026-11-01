"""
认证与凭据相关异常体系。
"""

class AuthError(Exception):
    """认证根异常"""
    pass


class CredentialError(AuthError):
    """凭据文件或配置异常（如文件不存在、无权限读取）"""
    pass


class InvalidCredentialError(CredentialError):
    """凭据内容损坏或缺少必要字段（如缺 signDigital、账号为空）"""
    pass


class BusinessAuthError(AuthError):
    """服务端业务认证失败（如密码错误、账号不存在、签名失效 50004）"""
    def __init__(self, message: str, code: int = -1):
        super().__init__(message)
        self.code = code


class TokenAcquisitionError(AuthError):
    """服务端响应成功但未能有效解析或提取 Token（如缺少 accessToken / refreshToken）"""
    pass


class UserInfoError(AuthError):
    """用户信息提取失败"""
    pass


class SchoolInfoError(AuthError):
    """学校信息提取失败"""
    pass


class SemesterInfoError(AuthError):
    """学期信息提取失败"""
    pass


class AuthNetworkError(AuthError):
    """认证过程网络通信异常（超时、连接失败、5xx 故障等）"""
    def __init__(self, message: str, http_status: int = -1):
        super().__init__(message)
        self.http_status = http_status
