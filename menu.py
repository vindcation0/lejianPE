"""乐健抢课工具 · 中文菜单"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

ACTIONS = [
    ("1", "查看 token 状态", ["lejian.py", "status"]),
    ("2", "查看可抢活动（已按规则过滤）", ["lejian.py", "list"]),
    ("3", "开始抢课（等到周日 14:00）", ["lejian.py", "grab"]),
    ("4", "立即抢课（不等周日）", ["lejian.py", "grab", "now"]),
    ("5", "自检 20 秒（不会提交）", ["lejian.py", "grab", "test"]),
    ("6", "查看课表与周次", ["lejian.py", "timetable"]),
    ("t", "可视化编辑课表（网页版）", ["tools/editor_server.py"]),
    ("7", "查看所有课程名（填配置用）", ["lejian.py", "names"]),
    ("8", "强制刷新 token", ["lejian.py", "refresh"]),
    ("9", "账号密码登录（命令行·主要方式）", ["lejian.py", "login"]),
    ("b", "重新初始化认证（一键修复凭据与会话）", ["lejian.py", "reinit"]),
    ("s", "系统配置向导（环境自检与首次初始化）", ["lejian.py", "setup"]),
    ("d", "系统健康体检（只读诊断报告）", ["lejian.py", "doctor"]),
    ("a", "从模拟器 App 同步 Token（备用方案）", ["tools/resync_from_app.py"]),
]


def draw():
    os.system("cls")
    print("=" * 50)
    print("        乐健体育 · 课外活动抢课工具")
    print("=" * 50)
    if not os.path.exists(os.path.join(ROOT, "credential.json")):
        print("   💡 首次使用提示：当前未检测到凭据，建议先选 [s] 运行配置向导")
    print()
    for key, label, _ in ACTIONS:
        print(f"   [{key}]  {label}")
    print("   [c]  打开配置文件 config.json")
    print("   [0]  退出")
    print()


def main():
    while True:
        try:
            draw()
            choice = input("请选择: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if choice in ("0", "q"):
            return

        if choice == "c":
            path = os.path.join(ROOT, "config.json")
            try:
                os.startfile(path)
            except Exception:
                pass
            print(f"\n配置文件位置：{path}")
            print("改完记得保存，然后回这里重新选。")
            input("按回车返回菜单…")
            continue

        hit = next((a for a in ACTIONS if a[0] == choice), None)
        if not hit:
            continue

        print()
        try:
            subprocess.call([PY] + hit[2], cwd=ROOT)
        except KeyboardInterrupt:
            pass
        print()
        input("按回车返回菜单…")


if __name__ == "__main__":
    main()
