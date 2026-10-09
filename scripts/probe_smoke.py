# -*- coding: utf-8 -*-
"""冒烟探针：ingest mock → 五档报告 → 缓存/记忆/基线 全链路验证（临时脚本）。"""
import sys, tempfile, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_config
from app.store.database import Database
from app.ingest import ingest_source
from app.ingest.mock_source import MockSource
from app.memory.baseline import refresh_rolling_after_import, create_anchor_snapshot
from app.reports.window import window_bounds, window_of_shot

tmp = tempfile.mkdtemp()
db_path = os.path.join(tmp, "probe.db")
db = Database(db_path)
cfg = get_config()

stats = ingest_source(db, MockSource(), on_imported=refresh_rolling_after_import)
print("ingest stats:", stats)

rows = db._query("SELECT session_id, session_time_utc FROM session_dim ORDER BY session_time_utc")
print("sessions:")
for r in rows:
    print(" ", r["session_id"], r["session_time_utc"])
    wk = window_of_shot(cfg, r["session_time_utc"], "weekly")
    print("    -> weekly:", wk, "| monthly:", window_of_shot(cfg, r["session_time_utc"], "monthly"),
          "| quarterly:", window_of_shot(cfg, r["session_time_utc"], "quarterly"),
          "| yearly:", window_of_shot(cfg, r["session_time_utc"], "yearly"))

# 窗口边界验证
for key in ("2026-W32", "2026-W33", "2026-W34", "2026-W35"):
    lo, hi = window_bounds(cfg, "weekly", key)
    print("weekly", key, "->", lo, "->", hi)
for key in ("2026-08", "2026-09"):
    lo, hi = window_bounds(cfg, "monthly", key)
    print("monthly", key, "->", lo, "->", hi)
for key in ("2026Q3",):
    lo, hi = window_bounds(cfg, "quarterly", key)
    print("quarterly", key, "->", lo, "->", hi)
lo, hi = window_bounds(cfg, "yearly", "2026")
print("yearly 2026 ->", lo, "->", hi)

# 锚点
sid0 = rows[0]["session_id"]
try:
    snap = create_anchor_snapshot(db, "1963169497552654337", "反曲弓", sid0)
    snap_id = db.insert_baseline_snapshot(snap)
    print("anchor created:", snap_id, snap["n_shots"], snap["distance_m"], snap["mode_composition"], snap["avg_wind"])
except ValueError as e:
    print("anchor FAILED:", e)

# 报告生成
from app.reports.generator import generate_report
from app.memory.notes import add_note

add_note(db, "1963169497552654337", "injury", "右肩轻微酸痛，训练量控制", "athlete", "1963169497552654337")
add_note(db, "1963169497552654337", "coach_note", "撒放节奏偏快，建议降速", "coach", "1963169497552654337")

aid = "1963169497552654337"
for gran, kw in [("daily", {"session_id": sid0}),
                 ("weekly", {"week": "2026-W32"}),
                 ("monthly", {"month": "2026-08"}),
                 ("quarterly", {"quarter": "2026Q3"}),
                 ("yearly", {"year": "2026"})]:
    try:
        rep = generate_report(db, aid, gran, kw["week"] if gran == "weekly" else kw.get("month") or kw.get("quarter") or kw.get("year") or f"daily:{sid0}", session_id=kw.get("session_id"), view="coach", force=True)
        print(f"[{gran}] ok sections={[s['key'] for s in rep['sections']]} cached={rep.get('cached')}")
        for s in rep["sections"]:
            print("   -", s["key"], "|", s["content"][:1])
    except Exception as e:
        print(f"[{gran}] FAILED:", type(e).__name__, e)

# 缓存命中
rep2 = generate_report(db, aid, "weekly", "2026-W32", view="coach")
print("cache hit:", rep2.get("cached"), rep2.get("report_id"))

print("memories:", db._query("SELECT conclusion_key, delta_value, judge_basis FROM report_memories")[:5])
print("snapshots:", [(r["snap_type"], r["n_shots"], r["avg_score"]) for r in db.list_snapshots(aid)])
db.close()
