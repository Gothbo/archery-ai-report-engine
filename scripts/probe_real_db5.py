# -*- coding: utf-8 -*-
"""IsGood/Score 语义核对（临时脚本）。"""
import sqlite3

db_path = r"D:\射箭最新程序202609\display_sys\SQLiteTest.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

def rows(sql):
    return [list(r) for r in cur.execute(sql).fetchall()]

print("Score=0:", rows("SELECT COUNT(*) FROM ScoreInfoNew WHERE CAST(Score AS REAL)=0"))
print("Score<1:", rows("SELECT COUNT(*) FROM ScoreInfoNew WHERE CAST(Score AS REAL)<1 AND CAST(Score AS REAL)>0"))
print("IsGood=0 且 Score>0:", rows("SELECT COUNT(*) FROM ScoreInfoNew WHERE IsGood=0 AND CAST(Score AS REAL)>0"))
print("IsGood=1 分布:", rows("SELECT IsGood, MIN(CAST(Score AS REAL)), MAX(CAST(Score AS REAL)), AVG(CAST(Score AS REAL)) FROM ScoreInfoNew GROUP BY IsGood"))
print("IsGood x ProjectId:", rows("SELECT ProjectId, IsGood, COUNT(*) FROM ScoreInfoNew GROUP BY ProjectId, IsGood"))
print("HitNum x IsGood:", rows("SELECT IsGood, HitNum, COUNT(*) FROM ScoreInfoNew GROUP BY IsGood, HitNum"))
print("Score=0 样例:", rows("SELECT Id, Score, IsGood, ShootingTime, IdentityID FROM ScoreInfoNew WHERE CAST(Score AS REAL)=0 LIMIT 3"))
conn.close()
