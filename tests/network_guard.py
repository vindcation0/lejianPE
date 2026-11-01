"""
测试套件全局网络隔离屏障 (Network Guard)
=======================================
安全准则：
- 允许本地回环通信 (127.0.0.1, localhost, ::1, 127.0.0.0/8, Unix Domain Socket)
- 严格剔除通配地址 (0.0.0.0, ::) 作为远程连接目标
- 拦截全量 Python 网络入口：
  - socket.socket.connect
  - socket.socket.connect_ex
  - socket.socket.sendto (UDP)
  - socket.getaddrinfo (DNS)
  - socket.gethostbyname (DNS)
  - socket.gethostbyname_ex (DNS)
  - socket.create_connection
  - http.client.HTTPConnection.connect / request (代理隧道防御)
- 在真实建立连接或发出 DNS 查询前立即拦截并抛出 RuntimeError
"""
import http.client
import ipaddress
import os
import socket
import urllib.parse
from typing import Any, Optional, Tuple

_orig_socket_connect = socket.socket.connect
_orig_socket_connect_ex = socket.socket.connect_ex
_orig_socket_sendto = socket.socket.sendto
_orig_getaddrinfo = socket.getaddrinfo
_orig_gethostbyname = socket.gethostbyname
_orig_gethostbyname_ex = socket.gethostbyname_ex
_orig_create_connection = socket.create_connection
_orig_http_connect = http.client.HTTPConnection.connect
_orig_http_request = http.client.HTTPConnection.request

_guard_installed = False


def is_local_host(host: Optional[str]) -> bool:
    """判断目标主机是否属于允许的本地回环范围 (严格排除 0.0.0.0 和 :: 通配地址)"""
    if not host:
        return False
    h = str(host).strip().lower()
    if h in ("localhost", "127.0.0.1", "::1"):
        return True

    # 检查是否为 loopback IP (如 127.0.0.0/8 范围内或 IPv6 回环)
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback
    except ValueError:
        pass

    return False


def guarded_connect(self, address: Any) -> Any:
    """拦截 socket.socket.connect"""
    if isinstance(address, tuple) and len(address) >= 2:
        host = address[0]
        if not is_local_host(host):
            raise RuntimeError(
                f"测试隔离屏障触发：单元测试严禁连接外部网络！目标: {address}"
            )
    elif isinstance(address, (str, bytes)):
        # Unix Domain Socket 路径，本地进程间通信，允许
        pass

    return _orig_socket_connect(self, address)


def guarded_connect_ex(self, address: Any) -> int:
    """拦截 socket.socket.connect_ex，在真正连接前阻断外部目标"""
    if isinstance(address, tuple) and len(address) >= 2:
        host = address[0]
        if not is_local_host(host):
            raise RuntimeError(
                f"测试隔离屏障触发：单元测试严禁连接外部网络 (connect_ex)！目标: {address}"
            )
    elif isinstance(address, (str, bytes)):
        # Unix Domain Socket
        pass

    return _orig_socket_connect_ex(self, address)


def guarded_sendto(self, *args, **kwargs) -> int:
    """拦截 socket.socket.sendto，防止向外部 IP 发送 UDP 数据"""
    address = None
    if len(args) == 2:
        address = args[1]
    elif len(args) >= 3:
        address = args[2]
    elif "address" in kwargs:
        address = kwargs["address"]

    if address:
        if isinstance(address, tuple) and len(address) >= 2:
            host = address[0]
            if not is_local_host(host):
                raise RuntimeError(
                    f"测试隔离屏障触发：单元测试严禁向外部地址发送 UDP 数据！目标: {address}"
                )
        elif isinstance(address, (str, bytes)):
            # Unix Domain Socket
            pass

    return _orig_socket_sendto(self, *args, **kwargs)


def guarded_getaddrinfo(
    host: Optional[str],
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> Any:
    """拦截 socket.getaddrinfo，禁止任何外部 DNS 查询"""
    if host and not is_local_host(host):
        raise RuntimeError(
            f"测试隔离屏障触发：单元测试严禁解析或访问外部域名！目标: {host}"
        )

    return _orig_getaddrinfo(host, port, family, type, proto, flags)


def guarded_gethostbyname(hostname: str) -> str:
    """拦截 socket.gethostbyname"""
    if not is_local_host(hostname):
        raise RuntimeError(
            f"测试隔离屏障触发：单元测试严禁解析外部域名 (gethostbyname)！目标: {hostname}"
        )
    return _orig_gethostbyname(hostname)


def guarded_gethostbyname_ex(hostname: str) -> Tuple[str, list, list]:
    """拦截 socket.gethostbyname_ex"""
    if not is_local_host(hostname):
        raise RuntimeError(
            f"测试隔离屏障触发：单元测试严禁解析外部域名 (gethostbyname_ex)！目标: {hostname}"
        )
    return _orig_gethostbyname_ex(hostname)


def guarded_create_connection(
    address: Tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: Any = None,
) -> socket.socket:
    """拦截 socket.create_connection"""
    host = address[0]
    if not is_local_host(host):
        raise RuntimeError(
            f"测试隔离屏障触发：单元测试严禁创建外部网络连接！目标: {address}"
        )
    return _orig_create_connection(
        address, timeout=timeout, source_address=source_address
    )


def guarded_http_connect(self) -> None:
    """拦截 http.client.HTTPConnection.connect，防止代理隧道穿透"""
    target = getattr(self, "_tunnel_host", None) or self.host
    if not is_local_host(target):
        raise RuntimeError(
            f"测试隔离屏障触发：单元测试严禁连接外部 HTTP/HTTPS 服务！目标: {target}"
        )
    return _orig_http_connect(self)


def guarded_http_request(
    self, method: str, url: str, body: Any = None, headers: Any = None, *, encode_chunked: bool = False
) -> None:
    """拦截 http.client.HTTPConnection.request，防止明文代理请求外部绝对 URL"""
    if url.startswith("http://") or url.startswith("https://"):
        parsed = urllib.parse.urlparse(url)
        if not is_local_host(parsed.hostname):
            raise RuntimeError(
                f"测试隔离屏障触发：单元测试严禁请求外部网络资源！目标: {url}"
            )
    return _orig_http_request(
        self, method, url, body=body, headers=headers or {}, encode_chunked=encode_chunked
    )


def install_network_guard():
    """安装全局测试网络隔离屏障"""
    global _guard_installed
    if _guard_installed:
        return

    # 禁用环境变量中可能存在的系统代理，避免干扰纯离线测试
    os.environ["no_proxy"] = "*"

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.sendto = guarded_sendto
    socket.getaddrinfo = guarded_getaddrinfo
    socket.gethostbyname = guarded_gethostbyname
    socket.gethostbyname_ex = guarded_gethostbyname_ex
    socket.create_connection = guarded_create_connection
    http.client.HTTPConnection.connect = guarded_http_connect
    http.client.HTTPConnection.request = guarded_http_request
    _guard_installed = True


def uninstall_network_guard():
    """卸载网络隔离屏障 (恢复系统原生方法)"""
    global _guard_installed
    if not _guard_installed:
        return

    socket.socket.connect = _orig_socket_connect
    socket.socket.connect_ex = _orig_socket_connect_ex
    socket.socket.sendto = _orig_socket_sendto
    socket.getaddrinfo = _orig_getaddrinfo
    socket.gethostbyname = _orig_gethostbyname
    socket.gethostbyname_ex = _orig_gethostbyname_ex
    socket.create_connection = _orig_create_connection
    http.client.HTTPConnection.connect = _orig_http_connect
    http.client.HTTPConnection.request = _orig_http_request
    _guard_installed = False


def is_network_guard_installed() -> bool:
    return _guard_installed
