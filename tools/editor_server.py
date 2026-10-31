"""
课表可视化编辑器本地 HTTP 服务 (tools/editor_server.py)
=====================================================
启动仅监听 127.0.0.1 的轻量本地 Web 服务，自动打开默认浏览器供用户交互式编辑课表。
支持随机会话令牌鉴权、数据实时校验、自动备份与原子持久化。
"""
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import timetable
from timetable.validator import ValidationError

HTML_PATH = os.path.join(ROOT, "timetable", "editor.html")
TIMETABLE_PATH = os.path.join(ROOT, "timetable.json")


def find_free_port(start_port=18234):
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


class EditorHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # 静默常规访问日志，仅异常时输出
        pass

    def check_token(self, parsed_url) -> bool:
        query = urllib.parse.parse_qs(parsed_url.query)
        req_token = query.get("token", [""])[0]
        if not req_token:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                req_token = auth[7:].strip()
        return req_token == self.server.session_token

    def send_json(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # 首页
        if parsed.path in ("/", "/index.html"):
            if not os.path.exists(HTML_PATH):
                self.send_error(404, "editor.html not found")
                return
            with open(HTML_PATH, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # 读取课表 API
        if parsed.path == "/api/load":
            if not self.check_token(parsed):
                self.send_json(403, {"error": "Token 无效或已过期"})
                return
            try:
                tt = timetable.load_timetable_file(self.server.timetable_path)
                self.send_json(200, tt.to_dict())
            except Exception as e:
                self.send_json(500, {"error": f"读取课表失败: {e}"})
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/save":
            if not self.check_token(parsed):
                self.send_json(403, {"error": "Token 无效或已过期"})
                return

            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))

                timetable.save_timetable_file(self.server.timetable_path, data, backup=True)
                slots_count = len(data.get("busy_slots") or [])
                self.send_json(200, {"success": True, "count": slots_count})
            except ValidationError as ve:
                self.send_json(400, {"success": False, "error": str(ve)})
            except Exception as e:
                self.send_json(500, {"success": False, "error": f"保存失败: {e}"})
            return

        if parsed.path == "/api/exit":
            if not self.check_token(parsed):
                self.send_json(403, {"error": "Token 无效或已过期"})
                return

            self.send_json(200, {"success": True})
            threading.Thread(target=self.server.shutdown).start()
            return

        self.send_error(404, "Not Found")


class EditorServer(http.server.HTTPServer):
    def __init__(self, server_address, session_token, timetable_path=TIMETABLE_PATH):
        self.session_token = session_token
        self.timetable_path = timetable_path
        super().__init__(server_address, EditorHandler)


def run_editor(open_browser=True):
    port = find_free_port()
    if not port:
        print("[错误] 未能找到可用本地端口")
        return

    token = secrets.token_hex(16)
    server = EditorServer(("127.0.0.1", port), token)
    url = f"http://127.0.0.1:{port}/?token={token}"

    print("=" * 60)
    print("    乐健体育 · 课表时间占用可视化编辑器")
    print("=" * 60)
    print(f"\n本地服务已启动: {url}")
    print("正在为你自动打开浏览器，若未打开可手动点击上方链接。\n")
    print("编辑完成后请在页面中点击「完成退出」或在此终端按 Ctrl+C 退出。\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("\n[退出] 课表编辑器服务已停止。")


if __name__ == "__main__":
    run_editor()
