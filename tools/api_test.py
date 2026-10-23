import sqlite3, json, urllib.request, urllib.error, ssl, sys, datetime

DB = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\DCStorage"
BASE = "https://cpes.legym.cn/"


def load_user():
    con = sqlite3.connect(DB)
    for (v,) in con.execute("SELECT value FROM DC_18286065_storage WHERE key='app-store'"):
        obj = json.loads(v)
        return obj["app"]["userInfo"]
    raise SystemExit("no app-store")


def call(path, body, token, school_id):
    url = BASE + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Organization", school_id)
    req.add_header("User-Agent", "okhttp/3.12.1")
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except Exception as e:
        return -1, repr(e)


u = load_user()
tok = u["accessToken"]
sid = u["schoolId"]
exp = u["accessTokenExpires"]
print("token expires:", datetime.datetime.fromtimestamp(exp / 1000).strftime("%Y-%m-%d %H:%M:%S"))
print("now          :", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("schoolId     :", sid)
print("semesterId   :", u.get("semesterId"))
print()

tests = [
    ("education/optional/course/time", {}),
    ("education/optional/course/list", {}),
    ("education/optional/course/newList", {}),
    ("education/optional/course/optionalPageList", {}),
    ("education/optional/course/project", {}),
]

for path, body in tests:
    code, text = call(path, body, tok, sid)
    print("=" * 70)
    print(f"POST {path}  -> HTTP {code}")
    print(text[:800] if text else "(empty)")
    print()
