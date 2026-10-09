# -*- coding: utf-8 -*-
"""M5 冒烟：真 SQLite 全量导入 → 报告生成（临时脚本）。"""
import sys, tempfile, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_config
from app.store.database import Database
from app.ingest import ingest_source
from app.ingest.sqlite_source import SQLiteSource, athlete_id_of
from app.memory.baseline import refresh_rolling_after_import

db_path = os.path.join(tempfile.mkdtemp(), "real.db")
db = Database(db_path)
src = SQLiteSource()

profiles = src.athlete_profiles()
print("profiles:", len(profiles), list(profiles.items())[:3])

stats = ingest_source(db, src, on_imported=refresh_rolling_after_import)
print("ingest stats:", stats)

print("sessions:", db._query("SELECT COUNT(*) FROM session_dim")[0][0])
print("shots:", db._query("SELECT COUNT(*) FROM shot_fact")[0][0])
print("athletes:", db._query("SELECT DISTINCT athlete_id FROM shot_fact"))
print("session sample:", db._query("SELECT session_id, athlete_id, session_time_utc, distance_m, shot_count, avg_wind FROM session_dim LIMIT 3"))
print("shot sample:", db._query("SELECT athlete_id, score, hit, x_mm, y_mm, mcr_t, hr, wind_speed, shooting_mode, bow_type, shot_time_utc FROM shot_fact LIMIT 3"))
print("rolling:", db._query("SELECT athlete_id, bow_type, n_shots, avg_score, collected_at_utc FROM baseline_snapshots WHERE snap_type='rolling' LIMIT 5"))

# 挑一个 2024+ 有 HR 的运动员（422802198808030396）测报告
aid = athlete_id_of("422802198808030396")
print("\ntest athlete:", aid)
from app.reports.window import window_of_shot
rows = db._query("SELECT DISTINCT session_time_utc FROM session_dim WHERE athlete_id=?", (aid,))
for r in rows[:3]:
    print("  session time:", r["session_time_utc"], "-> weekly", window_of_shot(get_config(), r["session_time_utc"], "weekly"))

from app.reports.generator import generate_report
for gran, key in [("weekly", "2024-W38"), ("monthly", "2024-09"), ("quarterly", "2024Q4"), ("yearly", "2024")]:
    try:
        rep = generate_report(db, aid, gran, key, view="coach", force=True)
        print(f"[{gran} {key}] sections={[s['key'] for s in rep['sections']]}")
        for s in rep["sections"]:
            print("   -", s["key"], "|", str(s["content"][:1])[:100])
    except Exception as e:
        print(f"[{gran} {key}] FAILED:", type(e).__name__, e)

db.close()
