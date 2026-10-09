# -*- coding: utf-8 -*-
"""真 SQLite 语义分布（临时脚本）。"""
import sqlite3

db_path = r"D:\射箭最新程序202609\display_sys\SQLiteTest.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

def rows(sql):
    return [list(r) for r in cur.execute(sql).fetchall()]

print("ShotType x ProjectId:", rows("""SELECT ProjectId, ShotType, COUNT(*) FROM ScoreInfoNew GROUP BY ProjectId, ShotType ORDER BY ProjectId"""))
print("ShotType x MatchType:", rows("""SELECT MatchType, ShotType, COUNT(*) FROM ScoreInfoNew GROUP BY MatchType, ShotType"""))
print("按年:", rows("""SELECT SUBSTR(ShootingTime,1,4), COUNT(*) FROM ScoreInfoNew GROUP BY SUBSTR(ShootingTime,1,4) ORDER BY 1"""))
print("2024-01 之后 vs 之前:", rows("""SELECT CASE WHEN ShootingTime >= '2024-01-01' THEN 'post2024' ELSE 'pre2024' END, COUNT(*) FROM ScoreInfoNew GROUP BY 1"""))
print("IdentityID 长度分布:", rows("""SELECT LENGTH(CAST(IdentityID AS TEXT)), COUNT(*) FROM ScoreInfoNew GROUP BY 1"""))
print("IdentityID 样例:", rows("SELECT DISTINCT IdentityID FROM ScoreInfoNew LIMIT 5"))
print("RegisterNum vs IdentityID 是否一致:", rows("""SELECT COUNT(*) FROM ScoreInfoNew WHERE RegisterNum <> IdentityID"""))
print("10环以上(内十):", rows("SELECT COUNT(*) FROM ScoreInfoNew WHERE CAST(Score AS REAL) >= 10.0"))
print("Score=10.9:", rows("SELECT COUNT(*) FROM ScoreInfoNew WHERE CAST(Score AS REAL) = 10.9"))
conn.close()
