import sqlite3, json

DB = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\DCStorage"
SENSITIVE = {"accessToken", "refreshToken", "mobile", "realName", "id",
             "organizationUserNumber", "campusId", "schoolId", "semesterId",
             "organizationId", "birthDay", "height", "weight"}

con = sqlite3.connect(DB)
for (v,) in con.execute("SELECT value FROM DC_18286065_storage WHERE key='app-store'"):
    obj = json.loads(v)
    break

user = obj["app"]["userInfo"]
print("userInfo 字段总数:", len(user))
print()
for k in sorted(user):
    val = user[k]
    if k in SENSITIVE:
        print(f"  {k:28} = <已隐藏>  ({type(val).__name__})")
    else:
        s = json.dumps(val, ensure_ascii=False)
        print(f"  {k:28} = {s[:120]}")

print()
print("app store 顶层键:", list(obj.get("app", {}).keys()))
