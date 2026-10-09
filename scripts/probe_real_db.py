# -*- coding: utf-8 -*-
"""M6 端到端验收探针：只读真库结构统计（不写入）。"""
import sqlite3

conn = sqlite3.connect(r"D:\射箭最新程序202609\display_sys\SQLiteTest.db")
print("score rows:", conn.execute("SELECT COUNT(*) FROM ScoreInfoNew").fetchone()[0])
print("hr rows:", conn.execute("SELECT COUNT(*) FROM HeartRateData").fetchone()[0])
print("wind rows:", conn.execute("SELECT COUNT(*) FROM WindSpeedDirection").fetchone()[0])
print("distinct identities>=17:", conn.execute(
    "SELECT COUNT(DISTINCT IdentityID) FROM ScoreInfoNew WHERE LENGTH(CAST(IdentityID AS TEXT))>=17").fetchone()[0])
print("min time:", conn.execute("SELECT MIN(ShootingTime) FROM ScoreInfoNew").fetchone()[0])
print("max time:", conn.execute("SELECT MAX(ShootingTime) FROM ScoreInfoNew").fetchone()[0])
