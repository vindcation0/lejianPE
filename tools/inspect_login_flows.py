import json

P = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\login_flows.jsonl"
rows = []
for line in open(P, encoding="utf-8"):
    if "manage/login" not in line:
        continue
    rows.append(json.loads(line))

print(f"登录请求共 {len(rows)} 条")
for i, r in enumerate(rows, 1):
    body = r.get("req_body") or ""
    print(f"  #{i}  {r.get('method')}  status={r.get('status')}  req_len={len(body)}")

if rows:
    body = (rows[-1].get("req_body") or "").strip()
    obj = json.loads(body)
    print("\n请求外层字段:", sorted(obj.keys()))
    print("响应状态:", rows[-1].get("status"))
    resp = rows[-1].get("resp_body") or ""
    print("响应长度:", len(resp))
    try:
        ro = json.loads(resp)
        print("响应外层字段:", sorted(ro.keys()))
    except Exception:
        print("响应不是 JSON")
