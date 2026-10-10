# -*- coding: utf-8 -*-
"""演示环境初始化：清空张明旧 mock 数据 → 导入 14 周扩展数据 → 建立锚点（S101）。

幂等：可重复执行。只动张明（1963169497552654337），不影响真库导入的其他运动员。
用法：python scripts/demo_reset.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.ingest import ingest_source
from app.ingest.mock_source import MockSource
from app.memory.baseline import create_anchor_snapshot, refresh_rolling_after_import
from app.store.database import get_database

ATHLETE_ID = "1963169497552654337"
MOCK_PATH = Path(__file__).resolve().parent.parent / "mock_data" / "训练数据集_14周_张明.json"
ANCHOR_SESSION = "S101"


def main():
    cfg = get_config()
    db = get_database(cfg)

    # 1) 清空张明旧数据（幂等：可能不存在）
    deleted_shots = db._query("SELECT COUNT(*) AS c FROM shot_fact WHERE athlete_id=?", (ATHLETE_ID,))[0]["c"]
    for table in ("shot_fact", "session_dim", "baseline_snapshots", "report_cache",
                  "report_memories", "memory_notes"):
        db._execute(f"DELETE FROM {table} WHERE athlete_id=?", (ATHLETE_ID,))
    print(f"1) 已清空张明旧数据（原箭数 {deleted_shots}）")

    # 2) 导入 14 周扩展数据
    source = MockSource(MOCK_PATH)
    stats = ingest_source(db, source, on_imported=refresh_rolling_after_import)
    print(f"2) 导入完成：{stats}")

    # 3) 建立锚点（S101 入队测试场，可比性元数据由 session_dim 提供）
    snap = create_anchor_snapshot(db, ATHLETE_ID, "反曲弓", ANCHOR_SESSION)
    db.insert_baseline_snapshot(snap)
    print(f"3) 锚点已建立：avg_score={snap['avg_score']} mcrT={snap['mcr_t']} "
          f"dispersion={snap['dispersion_mm']} hrVol={snap['hr_volatility']} "
          f"source={snap['source_session_id']} dist={snap['distance_m']}m wind={snap['avg_wind']}")

    # 4) 校验
    n = db._query("SELECT COUNT(*) AS c FROM shot_fact WHERE athlete_id=?", (ATHLETE_ID,))[0]["c"]
    anchors = db._query("SELECT COUNT(*) AS c FROM baseline_snapshots WHERE athlete_id=? AND snap_type='anchor'",
                        (ATHLETE_ID,))[0]["c"]
    rollings = db._query("SELECT COUNT(*) AS c FROM baseline_snapshots WHERE athlete_id=? AND snap_type='rolling'",
                         (ATHLETE_ID,))[0]["c"]
    print(f"4) 校验：张明箭数 {n}（预期 43 场 × 30 = 1290）、锚点 {anchors}、滚动快照 {rollings}")


if __name__ == "__main__":
    main()
