import sqlite3, json, os

db_path = r"C:\Users\vindacation\Documents\deepseek-harness\lejianPE\captures\DCStorage"
db = sqlite3.connect(db_path)
cur = db.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("tables:", tables)
print()

for t in tables:
    print("=" * 70)
    print("TABLE:", t)
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
    print("columns:", cols)
    rows = list(cur.execute(f"SELECT * FROM {t} LIMIT 100"))
    print("rows:", len(rows))
    for row in rows:
        print("-" * 60)
        for c, v in zip(cols, row):
            if isinstance(v, bytes):
                v = v.decode("utf-8", "ignore")
            s = str(v)
            print(f"  {c} = {s[:600]}")
