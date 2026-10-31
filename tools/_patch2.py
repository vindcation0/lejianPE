import io

p = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\lejian.py"
s = io.open(p, encoding="utf-8").read()
before = s

# check_conflict 用短超时
s = s.replace(
    '        code, obj = self.call("GET", "education/app/activity/checkConflict",\n'
    '                              {"activityId": activity_id})',
    '        code, obj = self.call("GET", "education/app/activity/checkConflict",\n'
    '                              {"activityId": activity_id}, timeout=4)')

# 允许关闭服务端冲突校验（省一次往返）
s = s.replace(
    '                if c.check_conflict(aid):',
    '                if cfg.get("check_conflict", True) and c.check_conflict(aid):')

# 失败时快速重试，而不是干等 poll 间隔
s = s.replace(
    '            log(f"  拉取活动失败: {e}")\n'
    '            time.sleep(poll)\n'
    '            continue',
    '            log(f"  拉取活动失败: {e}")\n'
    '            time.sleep(0.05)\n'
    '            continue')

io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched2:", "ok" if s != before else "NO CHANGE")
