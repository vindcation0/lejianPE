import io, json

# --- 1. 给 activities() 加内部重试 ---
p = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\lejian.py"
s = io.open(p, encoding="utf-8").read()
before = s

s = s.replace(
    '        while page <= 10:\n'
    '            code, obj = self.call("POST", "education/app/activity/getActivityList",\n'
    '                                  {"state": 1, "page": page, "size": page_size,\n'
    '                                   "semesterId": self.s["semesterId"]},\n'
    '                                  timeout=req_timeout)\n'
    '            if code != 200 or not isinstance(obj, dict):\n'
    '                break',
    '        while page <= 10:\n'
    '            code, obj = None, None\n'
    '            for _ in range(3):\n'
    '                code, obj = self.call("POST", "education/app/activity/getActivityList",\n'
    '                                      {"state": 1, "page": page, "size": page_size,\n'
    '                                       "semesterId": self.s["semesterId"]},\n'
    '                                      timeout=req_timeout)\n'
    '                if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:\n'
    '                    break\n'
    '                time.sleep(0.25)\n'
    '            if code != 200 or not isinstance(obj, dict):\n'
    '                break')

if s != before:
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)
    print("lejian.py: activities() 已加内部重试")
else:
    print("lejian.py: 未匹配到目标代码")

# --- 2. 配置恢复为安全默认值 ---
cp = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\config.json"
cfg = json.load(open(cp, encoding="utf-8"))
cfg["dry_run"] = True
cfg["check_conflict"] = True
cfg["poll_interval_ms"] = 500
cfg["now_seconds"] = 120
json.dump(cfg, open(cp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("config.json: dry_run=True, check_conflict=True, poll=500, now_seconds=120")
