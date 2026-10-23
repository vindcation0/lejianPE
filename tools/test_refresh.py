import sqlite3, json, urllib.request, urllib.error, ssl, datetime

DB = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\DCStorage"
OUT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\refresh_test.json"
BASE = "https://cpes.legym.cn/"


def load_user():
    con = sqlite3.connect(DB)
    for (v,) in con.execute("SELECT value FROM DC_18286065_storage WHERE key='app-store'"):
        return json.loads(v)["app"]["userInfo"]
    raise SystemExit("no app-store")


def dec(raw):
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


u = load_user()
rt = u["refreshToken"]
url = BASE + "authorization/user/refreshToken?refreshToken=" + rt
req = urllib.request.Request(url, method="GET")
req.add_header("User-Agent", "okhttp/3.12.1")

out = {"request_url": url.replace(rt, "<REFRESH_TOKEN>"), "http": None, "response": None}

try:
    with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
        out["http"] = r.status
        out["response"] = dec(r.read())
except urllib.error.HTTPError as e:
    out["http"] = e.code
    out["response"] = dec(e.read())
except Exception as e:
    out["http"] = -1
    out["response"] = repr(e)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print("http:", out["http"])
print("written:", OUT)
