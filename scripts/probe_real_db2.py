# -*- coding: utf-8 -*-
"""探查真 SQLite 细节：UserInfo / WindSpeedDirection 全列 + ScoreInfoNew 维度分布（临时脚本）。"""
import sqlite3, json

db_path = r"D:\射箭最新程序202609\display_sys\SQLiteTest.db"
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

for t in ("UserInfo", "WindSpeedDirection", "DeviceInfo"):
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info('{t}')").fetchall()]
    print(f"== {t} cols:", cols)
    try:
        rows = cur.execute(f"SELECT * FROM '{t}' LIMIT 3").fetchall()
        for r in rows:
            print("  ", json.dumps(dict(r), ensure_ascii=False, default=str)[:400])
    except Exception as e:
        print("  err:", e)

print("\n== ScoreInfoNew 统计 ==")
print("count:", cur.execute("SELECT COUNT(*) FROM ScoreInfoNew").fetchone()[0])
print("ShotType:", cur.execute("SELECT ShotType, COUNT(*) FROM ScoreInfoNew GROUP BY ShotType").fetchall())
print("IdentityIDs:", cur.execute("SELECT IdentityID, COUNT(*) FROM ScoreInfoNew GROUP BY IdentityID").fetchall())
print("ShootingTime range:", cur.execute("SELECT MIN(ShootingTime), MAX(ShootingTime) FROM ScoreInfoNew").fetchone())
print("ProjectId:", cur.execute("SELECT ProjectId, COUNT(*) FROM ScoreInfoNew GROUP BY ProjectId").fetchall())
print("MatchType:", cur.execute("SELECT MatchType, COUNT(*) FROM ScoreInfoNew GROUP BY MatchType").fetchall())
print("ShootingTime nulls:", cur.execute("SELECT COUNT(*) FROM ScoreInfoNew WHERE ShootingTime IS NULL").fetchone()[0])
print("MCRT? 无此列（撒放用时在弹道消息，不在本库）")
conn.close()
