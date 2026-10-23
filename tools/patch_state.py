"""把 refresh_test.json 里刷出来的新 token 写回 state.json。"""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state.json")
TEST = os.path.join(ROOT, "captures", "refresh_test.json")

with open(TEST, encoding="utf-8") as f:
    res = json.load(f)["response"]
data = json.loads(res)["data"]

with open(STATE, encoding="utf-8") as f:
    st = json.load(f)

st["accessToken"] = data["accessToken"]
st["refreshToken"] = data["refreshToken"]
st["accessTokenExpires"] = data["accessTokenExpires"]

with open(STATE, "w", encoding="utf-8") as f:
    json.dump(st, f, ensure_ascii=False, indent=2)

print("state.json 已更新")
