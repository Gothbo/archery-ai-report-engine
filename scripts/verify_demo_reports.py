# -*- coding: utf-8 -*-
"""demo 配置下验证五档报告效果：打印各档报告的结构化结论段落。

用法（需 ENGINE_CONFIG 指向 config.demo.json）：
    python scripts/verify_demo_reports.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.reports.generator import generate_report
from app.store.database import get_database

ATHLETE_ID = "1963169497552654337"

# (粒度, 窗口键, 备注)
CASES = [
    ("weekly", "2026-W34", "平台期（W33-W36 均环 10.12~10.16，差值稳定超 MDC）"),
    ("weekly", "2026-W40", "再进步（均环 10.16+，含跨日早训场 S143）"),
    ("monthly", "2026-08", "八月（W32-W35：稳态→超 MDC 进步）"),
    ("monthly", "2026-09", "九月（W36-W39：平台→再进步）"),
    ("quarterly", "2026Q3", "完整季度（W27-W39，含入队测试锚点周）"),
    ("daily", "daily:S143", "跨日早训场（A1：UTC 22:00 → 北京次日 06:00）"),
]


def main():
    cfg = get_config()
    print(f"== 配置 ==")
    print(f"mdc_source={cfg.mdc_source}  timezone={cfg.timezone}")
    print(f"MDC: avgScore {cfg.mdc['avgScore'].threshold} dir={cfg.mdc['avgScore'].direction} / "
          f"mcrT {cfg.mdc['mcrT'].threshold} dir={cfg.mdc['mcrT'].direction} / "
          f"dispersionMm {cfg.mdc['dispersionMm'].threshold} dir={cfg.mdc['dispersionMm'].direction}")
    db = get_database(cfg)

    for granularity, window_key, note in CASES:
        report = generate_report(db, ATHLETE_ID, granularity, window_key, view="coach", force=True,
                                 session_id="S143" if granularity == "daily" else None)
        print(f"\n{'=' * 72}")
        print(f"[{report['granularity']}] {report['window_key']}  {note}")
        print(f"  report_id={report['report_id'][:8]}  anchor_rebuild_hint={report['coach_extra'].get('anchor_rebuild_hint')}")
        for sec in report["sections"]:
            lines = " / ".join(str(c) for c in sec["content"])
            print(f"  · [{sec['key']}] {sec['title']}: {lines}")


if __name__ == "__main__":
    main()
