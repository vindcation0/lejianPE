import sqlite3, json, urllib.request, urllib.error, ssl, datetime

DB = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\DCStorage"
BASE = "https://cpes.legym.cn/"


def load_user():
    con = sqlite3.connect(DB)
    for (v,) in con.execute("SELECT value FROM DC_18286065_storage WHERE key='app-store'"):
        return json.loads(v)["app"]["userInfo"]
    raise SystemExit("no app-store")


def dec(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def call(path, body, token, school_id):
    url = BASE + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Organization", school_id)
    req.add_header("User-Agent", "okhttp/3.12.1")
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:
            return r.status, dec(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dec(e.read())
    except Exception as e:
        return -1, repr(e)


u = load_user()
tok, sid = u["accessToken"], u["schoolId"]
uid = u["id"]
sem = u.get("semesterId")
print("user id      :", uid)
print("semesterId   :", sem)
print()

print("=" * 70)
print("当前学期")
c, t = call("education/semester/getCurrent", {}, tok, sid)
print(c, t[:600])
print()

year = str(datetime.datetime.now().year)
try:
    obj = json.loads(t)
    d = obj.get("data") or {}
    if isinstance(d, dict):
        year = str(d.get("year") or d.get("schoolYear") or year)
except Exception:
    pass
print("推断 year =", year)
print()

cases = [
    ("education/optional/course/newList",
     {"userId": uid, "year": year, "semesterId": sem}),
    ("education/optional/course/project",
     {"semesterId": sem}),
    ("education/optional/course/time",
     {"semesterId": sem}),
]
for path, body in cases:
    c, t = call(path, body, tok, sid)
    print("=" * 70)
    print(f"POST {path} -> HTTP {c}")
    print(t[:1200])
    print()
