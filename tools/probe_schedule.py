import json, os, ssl, urllib.request, urllib.error, urllib.parse, datetime

ROOT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE"
STATE = os.path.join(ROOT, "state.json")
OUT = os.path.join(ROOT, "captures", "schedule_probe.json")
BASE = "https://cpes.legym.cn/"

st = json.load(open(STATE, encoding="utf-8"))
CTX = ssl.create_default_context()


def dec(raw):
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def call(method, path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + st["accessToken"])
    req.add_header("Organization", st["schoolId"])
    req.add_header("User-Agent", "okhttp/3.12.1")
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            return r.status, dec(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dec(e.read())
    except Exception as e:
        return -1, repr(e)


uid, sem, year = st["userId"], st["semesterId"], st.get("year", "2026")
results = []

cases = [
    ("GET", "education/course/app/forStudent/list",
     {"userId": uid, "semesterId": sem}),
    ("GET", "education/course/app/forStudent/list",
     {"userId": uid, "semesterId": sem, "year": year}),
    ("GET", "education/course/today", {}),
    ("GET", "education/course/app/forStudent/userinfo", {}),
    ("GET", "education/app/forStudent/userinfo", {}),
]

for m, p, params in cases:
    code, text = call(m, p, params)
    results.append({"method": m, "path": p, "params": params,
                    "http": code, "response": text})
    print(f"[{code}] {p}")

json.dump(results, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("written:", OUT)
