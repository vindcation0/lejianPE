"""从模拟器里的 DCStorage 数据库生成 state.json（含 token）。"""
import sqlite3, json, os, sys

DB = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\DCStorage"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.json")


def main():
    con = sqlite3.connect(DB)
    user = None
    for (v,) in con.execute("SELECT value FROM DC_18286065_storage WHERE key='app-store'"):
        user = json.loads(v)["app"]["userInfo"]
        break
    if not user:
        sys.exit("没找到 app-store，请先在模拟器里登录一次 App")

    year_range = (user.get("yearRange") or "")
    state = {
        "accessToken": user["accessToken"],
        "refreshToken": user["refreshToken"],
        "accessTokenExpires": user["accessTokenExpires"],
        "userId": user["id"],
        "schoolId": user["schoolId"],
        "semesterId": user.get("semesterId"),
        "year": (year_range[:4] if year_range else "2026"),
        "mobile": user.get("mobile"),
        "realName": user.get("realName"),
        "organizationUserNumber": user.get("organizationUserNumber"),
        "campusId": user.get("campusId"),
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print("written:", OUT)
    print("schoolId   :", state["schoolId"])
    print("semesterId :", state["semesterId"])
    print("year       :", state["year"])


if __name__ == "__main__":
    main()
