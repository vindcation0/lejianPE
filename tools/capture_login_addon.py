"""mitmproxy 插件：把乐健相关的请求/响应逐条追加到 JSONL，进程被杀也不丢。"""
import json
import os
import time

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "captures", "login_flows.jsonl")


def response(flow):
    host = flow.request.pretty_host or ""
    if "legym" not in host:
        return
    try:
        rec = {
            "ts": round(time.time(), 3),
            "method": flow.request.method,
            "url": flow.request.pretty_url.split("?")[0],
            "status": flow.response.status_code if flow.response else None,
            "req_body": flow.request.get_text(strict=False),
            "resp_body": flow.response.get_text(strict=False) if flow.response else None,
        }
    except Exception as e:
        rec = {"ts": time.time(), "error": repr(e)}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
