import json, os, ssl, urllib.request, urllib.error
from datetime import datetime

ROOT = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE"
STATE = os.path.join(ROOT, "state.json")
OUT = os.path.join(ROOT, "captures", "activity_table.txt")
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


def ts(v):
    try:
        return datetime.fromtimestamp(int(v) / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return "-"


items = []
for page in range(1, 4):
    code, text = post("education/app/activity/getActivityList",
                      {"state": 1, "page": page, "size": 50, "semesterId": st["semesterId"]})
    obj = json.loads(text)
    d = obj.get("data") or {}
    batch = d.get("items") or []
    if not batch:
        break
    items.extend(batch)

lines = [f"共 {len(items)} 条", ""]
lines.append(f"{'会员':4}{'报名窗口':>20}  {'活动时间':>20}  {'名额':>10}  名称")
lines.append("-" * 120)

member_cnt = 0
for it in items:
    name = it.get("name") or ""
    addr = it.get("address") or ""
    is_member = ("会员制" in name) or ("需办会员" in addr)
    if is_member:
        member_cnt += 1
    reg = f"{ts(it.get('startRegisterTime'))}~{ts(it.get('endRegisterTime'))}"
    per = f"{ts(it.get('startTime'))}~{ts(it.get('endTime'))}"
    quota = f"{it.get('registerPeople')}/{it.get('limitPeopleNumber')}"
    lines.append(f"{'会员' if is_member else '普通':4}{reg:>20}  {per:>20}  {quota:>10}  {name}")

lines.append("")
lines.append(f"会员制 {member_cnt} 条 / 普通 {len(items)-member_cnt} 条")
open(OUT, "w", encoding="utf-8").write("\n".join(lines))
print("written:", OUT)
