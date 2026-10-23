import sqlite3, json, urllib.request, urllib.error, ssl, datetime

DB = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\DCStorage"
OUT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\api_probe.json"
BASE = "https://cpes.legym.cn/"

results = []


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


def req(method, path, body, token, sid):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None and method == "POST" else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer " + token)
    r.add_header("Organization", sid)
    r.add_header("User-Agent", "okhttp/3.12.1")
    try:
        with urllib.request.urlopen(r, timeout=20, context=ssl.create_default_context()) as resp:
            return resp.status, dec(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, dec(e.read())
    except Exception as e:
        return -1, repr(e)


def probe(method, path, body, token, sid, label):
    code, text = req(method, path, body, token, sid)
    item = {"label": label, "method": method, "path": path, "body": body,
            "http": code, "response": text}
    results.append(item)
    print(f"[{code}] {method} {path}")


u = load_user()
tok, sid, uid, sem = u["accessToken"], u["schoolId"], u["id"], u.get("semesterId")

semester_obj = None

probe("GET", "education/semester/getCurrent", None, tok, sid, "current semester")
try:
    semester_obj = json.loads(results[-1]["response"]).get("data")
except Exception:
    pass

for y in [str(datetime.datetime.now().year), "2026-2027", "2025-2026"]:
    probe("POST", "education/optional/course/newList",
          {"userId": uid, "year": y, "semesterId": sem}, tok, sid, f"newList year={y}")

probe("GET", f"education/optional/course/time?semesterId={sem}", None, tok, sid, "time GET")
probe("POST", "education/optional/course/time",
      {"semesterId": sem, "userId": uid, "year": "2026"}, tok, sid, "time POST+params")
probe("POST", "education/optional/course/optionalPageList",
      {"userId": uid, "semesterId": sem, "year": "2026"}, tok, sid, "optionalPageList")
probe("POST", "education/optional/course/list",
      {"userId": uid, "semesterId": sem, "year": "2026"}, tok, sid, "list (full params)")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump({"semester": semester_obj, "user": {k: u.get(k) for k in
               ("id", "schoolId", "semesterId", "identity", "organizationUserNumber", "campusId")},
               "results": results}, f, ensure_ascii=False, indent=2)
print("written:", OUT)
