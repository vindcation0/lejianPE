import json, io, collections

p = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\logs\events-20261101.jsonl"
rows = [json.loads(l) for l in io.open(p, encoding="utf-8") if l.strip()]

print(f"事件总数 {len(rows)}")
codes = collections.Counter(r.get("http") for r in rows if r["event"] == "list_fail")
print("失败按 HTTP 码:", dict(codes))

outages = []
fails = 0
for r in rows:
    if r["event"] == "list_fail":
        fails += 1
    elif r["event"] == "list_ok":
        outages.append(r.get("outage_sec", 0))

print(f"完整中断段 {len(outages)} 段，累计 {sum(outages)} 秒，最长 {max(outages) if outages else 0} 秒")

print("\n14:00 前后的中断段：")
for r in rows:
    if r["event"] == "list_ok":
        print(f"  {r['t']}  恢复，中断 {r.get('outage_sec')} 秒")

starts = [r["t"] for r in rows if r["event"] == "list_fail"]
if starts:
    print(f"\n最早失败 {starts[0]}，最晚失败 {starts[-1]}")
