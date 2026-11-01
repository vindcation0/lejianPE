"""端到端验证：用抓到的载荷 + 全新 t 走完整登录流程，并校验 state.json 是否可用。

密码仅在内存中用于构造请求，不打印、不落盘。
"""
import json, sys, time
sys.path.insert(0, r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\tools")
import login_cli as LC
from login_envelope import unwrap, wrap

P = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\login_flows.jsonl"
rec = next(json.loads(l) for l in open(P, encoding="utf-8") if "manage/login" in l)
old = unwrap(json.loads(rec["req_body"]))

cred = json.load(open(LC.CRED, encoding="utf-8"))

print("=" * 60)
print("步骤 1：用 credential.json 里的账号与签名 + 原始载荷里的密码 登录")
print("=" * 60)
data, err = LC.do_login(cred["account"], old["password"],
                        cred["signDigital"], cred["entrance"])
if not data:
    print("❌ 失败:", err)
    sys.exit(1)
print("✅ 成功")
print("  姓名      :", data.get("realName"))
print("  schoolId  :", data.get("schoolId"))
exp = data.get("accessTokenExpires")
print("  有效期至  :", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp / 1000)))

print()
print("=" * 60)
print("步骤 2：写入 state.json")
print("=" * 60)
st = LC.save_state(data)
print("  semesterId :", st.get("semesterId"))
print("  year       :", st.get("year"))

print()
print("=" * 60)
print("步骤 3：用新 state.json 调真实业务接口")
print("=" * 60)
import lejian as L
c = L.Client(L.load_json(L.STATE))
items = c.activities()
print("  活动列表拉取:", len(items), "条")
print("  token 剩余  :", int(c.expires_in()), "秒")
