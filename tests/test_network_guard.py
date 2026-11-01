"""
全局测试网络隔离专项测试套件 (N01 ~ N07)。
===========================================
验证测试网络隔离屏障在各种网络通信形态下的拦截与放行行为：
1. 允许 127.0.0.1
2. 允许 localhost
3. 允许 ::1
4. 拦截外部公网 IP 连接
5. 拦截外部域名解析
6. 允许 urllib 请求本地服务
7. 拦截 urllib 请求外部服务
"""
import http.server
import socket
import threading
import unittest
import urllib.error
import urllib.request

from tests.network_guard import (
    install_network_guard,
    is_network_guard_installed,
    uninstall_network_guard,
)


class LocalEchoHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "8")
        self.end_headers()
        self.wfile.write(b"OK_LOCAL")

    def log_message(self, format, *args):
        pass  # 静默控制台日志


class NetworkGuardTestSuite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 启动一个本地测试专用轻量级 HTTP 服务
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), LocalEchoHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()

        # 确保安装网络隔离屏障
        install_network_guard()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_N01_allow_connect_127_0_0_1(self):
        """N01: 允许连接 127.0.0.1 本地回环地址"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("127.0.0.1", self.port))
            s.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            chunks = []
            while True:
                c = s.recv(1024)
                if not c:
                    break
                chunks.append(c)
            data = b"".join(chunks)
            self.assertIn(b"OK_LOCAL", data)
        finally:
            s.close()

    def test_N02_allow_connect_localhost(self):
        """N02: 允许连接 localhost 本地主机名"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("localhost", self.port))
            s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            chunks = []
            while True:
                c = s.recv(1024)
                if not c:
                    break
                chunks.append(c)
            data = b"".join(chunks)
            self.assertIn(b"OK_LOCAL", data)
        finally:
            s.close()

    def test_N03_allow_ipv6_loopback(self):
        """N03: 允许 ::1 IPv6 回环地址解析与判定"""
        # getaddrinfo 对 ::1 必须正常放行，严禁抛出隔离异常
        res = socket.getaddrinfo("::1", self.port)
        self.assertTrue(len(res) > 0)

    def test_N04_block_public_ip_connection(self):
        """N04: 拦截外部公网 IP 连接尝试 (直接抛出 RuntimeError)"""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                s.connect(("8.8.8.8", 53))
            self.assertIn("测试隔离屏障触发", str(ctx.exception))
            self.assertIn("外部网络", str(ctx.exception))
        finally:
            s.close()

    def test_N05_block_external_domain_resolution(self):
        """N05: 拦截外部域名解析 (在发起真实 DNS 查询前立即阻断)"""
        for domain in ("cpes.legym.cn", "api.github.com", "example.com"):
            with self.assertRaises(RuntimeError) as ctx:
                socket.getaddrinfo(domain, 443)
            self.assertIn("测试隔离屏障触发", str(ctx.exception))
            self.assertIn(domain, str(ctx.exception))

    def test_N06_allow_urllib_local_request(self):
        """N06: urllib 通过 127.0.0.1 或 localhost 访问本地服务正常放行"""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
            self.assertIn("OK_LOCAL", body)

    def test_N07_block_urllib_external_request(self):
        """N07: urllib 发起外部网络请求被隔离屏障严格拦截"""
        req = urllib.request.Request("https://cpes.legym.cn/authorization/semester/currentSemester")
        with self.assertRaises(RuntimeError) as ctx:
            urllib.request.urlopen(req, timeout=1)
        self.assertIn("测试隔离屏障触发", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
