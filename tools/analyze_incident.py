import re, io, collections

p = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\logs\run-20261025.log"
lines = io.open(p, encoding="utf-8").read().splitlines()

codes = collections.Counter()
first = {}
last = {}
times = []

for ln in lines:
    m = re.match(r"\[(\d\d:\d\d:\d\d)\.\d+\]\s*(.*)$", ln)
    if not m:
        continue
    t, rest = m.group(1), m.group(2)
    times.append(t)
    for pat, label in [
        (r"HTTP 500", "HTTP 500"),
        (r"HTTP 502", "HTTP 502"),
        (r"HTTP 503", "HTTP 503"),
        (r"HTTP -1", "超时(-1)"),
        (r"Connection refused", "Connection refused"),
        (r"该类型活动已达到每周上限", "业务:每周上限"),
        (r"该类型活动已达到每日上限", "业务:每日上限"),
        (r"轮询中", "轮询心跳"),
        (r"拉取失败|拉取活动失败", "拉取失败"),
    ]:
        if re.search(pat, rest):
            codes[label] += 1
            first.setdefault(label, t)
            last[label] = t

# 每分钟事件量
per_min = collections.Counter(t[:5] for t in times)

print("日志时间范围:", times[0], "→", times[-1])
print("总行数:", len(lines))
print()
print(f"{'类型':<22}{'次数':>6}   {'首次':>9}  {'末次':>9}")
print("-" * 56)
for k, v in codes.most_common():
    print(f"{k:<22}{v:>6}   {first[k]:>9}  {last[k]:>9}")

print("\n每分钟日志行数（只列 13:55 之后）：")
for m in sorted(per_min):
    if m >= "13:55":
        print(f"  {m}  {'#' * min(per_min[m], 60)} {per_min[m]}")
