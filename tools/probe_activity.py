import json, os, ssl, urllib.request, urllib.error

ROOT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE"
STATE = os.path.join(ROOT, "state.json")
OUT = os.path.join(ROOT, "captures", "activity_probe.json")
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


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer " + st["accessToken"])
    req.add_header("Organization", st["schoolId"])
    req.add_header("User-Agent", "okhttp/3.12.1")
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return r.status, dec(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dec(e.read())
    except Exception as e:
        return -1, repr(e)


sem = st["semesterId"]
cases = [
    ("state=0 + semesterId", {"state": 0, "page": 1, "size": 50, "semesterId": sem}),
    ("state=1 + semesterId", {"state": 1, "page": 1, "size": 50, "semesterId": sem}),
    ("state=0 + day=7", {"state": 0, "page": 1, "size": 50, "day": 7}),
]

out = []
for label, body in cases:
    code, text = post("education/app/activity/getActivityList", body)
    out.append({"label": label, "body": body, "http": code, "response": text})
    print(f"[{code}] {label}  len={len(text)}")

json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("written:", OUT)
