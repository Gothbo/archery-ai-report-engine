# -*- coding: utf-8 -*-
"""真 SQLite 分布详情（临时脚本）。"""
import sqlite3, json

db_path = r"D:\射箭最新程序202609\display_sys\SQLiteTest.db"
conn = sqlite3.connect(db_path)
cur = conn.cursor()

def rows(sql):
    return [list(r) for r in cur.execute(sql).fetchall()]

print("ShotType:", rows("SELECT ShotType, COUNT(*) FROM ScoreInfoNew GROUP BY ShotType"))
print("MatchType:", rows("SELECT MatchType, COUNT(*) FROM ScoreInfoNew GROUP BY MatchType"))
print("ProjectId:", rows("SELECT ProjectId, COUNT(*) FROM ScoreInfoNew GROUP BY ProjectId"))
print("IdentityID count:", rows("SELECT COUNT(DISTINCT IdentityID) FROM ScoreInfoNew"))
print("IdentityID top:", rows("SELECT IdentityID, COUNT(*) c FROM ScoreInfoNew GROUP BY IdentityID ORDER BY c DESC LIMIT 5"))
print("Time range:", rows("SELECT MIN(ShootingTime), MAX(ShootingTime) FROM ScoreInfoNew"))
print("Score range:", rows("SELECT MIN(CAST(Score AS REAL)), MAX(CAST(Score AS REAL)), AVG(CAST(Score AS REAL)) FROM ScoreInfoNew"))
print("HitNum distinct:", rows("SELECT HitNum, COUNT(*) FROM ScoreInfoNew GROUP BY HitNum"))
print("IsGood:", rows("SELECT IsGood, COUNT(*) FROM ScoreInfoNew GROUP BY IsGood"))
print("Status:", rows("SELECT Status, COUNT(*) FROM ScoreInfoNew GROUP BY Status"))
print("GroupNum:", rows("SELECT GroupNum, COUNT(*) FROM ScoreInfoNew GROUP BY GroupNum LIMIT 10"))
print("Stage:", rows("SELECT Stage, COUNT(*) FROM ScoreInfoNew GROUP BY Stage"))
print("ShotDate nonzero:", rows("SELECT COUNT(*) FROM ScoreInfoNew WHERE ShotDate > '2000-01-01'"))
print("Num range:", rows("SELECT MIN(Num), MAX(Num) FROM ScoreInfoNew"))
print("\nHR count:", rows("SELECT COUNT(*) FROM HeartRateData"))
print("HR time range:", rows("SELECT MIN(CreateDate), MAX(CreateDate) FROM HeartRateData"))
print("HR IdentityID top:", rows("SELECT IdentityID, COUNT(*) c FROM HeartRateData GROUP BY IdentityID ORDER BY c DESC LIMIT 3"))
print("\nWind count:", rows("SELECT COUNT(*) FROM WindSpeedDirection"))
print("Wind time range:", rows("SELECT MIN(CreateTime), MAX(CreateTime) FROM WindSpeedDirection"))
print("Wind speed range:", rows("SELECT MIN(WindSpeed), MAX(WindSpeed) FROM WindSpeedDirection"))
print("Wind RegisterNum top:", rows("SELECT RegisterNum, COUNT(*) c FROM WindSpeedDirection GROUP BY RegisterNum ORDER BY c DESC LIMIT 3"))
# ScoreInfoNew 与 HR/Wind 的时间是否可关联（同一 IdentityID 同场）
print("\nScore 单 Identity 场次（按 ShootingTime 聚合）:")
print(rows("SELECT IdentityID, SUBSTR(ShootingTime,1,10), COUNT(*) FROM ScoreInfoNew GROUP BY IdentityID, SUBSTR(ShootingTime,1,10) ORDER BY 3 DESC LIMIT 6"))
conn.close()
