# -*- coding: utf-8 -*-
"""生成真实数据报告评审样本（当前代码）→ 输出/阶段1-真实报告评审/after/。

运动员 ID 通过环境变量 REAL_ATHLETE_ID 注入（避免在仓库内硬编码可还原身份号）。
用法：
    $env:REAL_ATHLETE_ID='<derived_id>'; py -3.14 scripts/gen_real_review_samples.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_config
from app.reports.generator import generate_report
from app.store.database import get_database

OUT = Path(__file__).resolve().parent.parent / "输出" / "阶段1-真实报告评审" / "after"


def _render_md(rep: dict) -> str:
    lines = [f"# {rep['granularity']} · {rep['window_key']} · view={rep['view']}", ""]
    lines.append(f"- 运动员：{rep['athlete']['name']}（{rep['athlete']['id']}）")
    lines.append(f"- 弓种：{rep.get('bow_type')}")
    lines.append(f"- 窗口：{rep.get('window_start')} → {rep.get('window_end')}")
    lines.append("")
    for sec in rep["sections"]:
        lines.append(f"## [{sec['key']}] {sec['title']}")
        for c in sec["content"]:
            lines.append(f"- {c}")
        lines.append("")
    lines.append("## 建议")
    if rep.get("suggestions"):
        for s in rep["suggestions"]:
            lines.append(f"- {s}")
    else:
        lines.append("- （无）")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    aid = os.environ.get("REAL_ATHLETE_ID")
    if not aid:
        print("跳过：未设置 REAL_ATHLETE_ID")
        return 1
    cfg = get_config()
    db = get_database(cfg)
    OUT.mkdir(parents=True, exist_ok=True)

    cases = [
        ("weekly", "2025-W01", None),
        ("weekly", "2025-W14", None),
        ("monthly", "2025-01", None),
        ("daily", f"daily:{aid}_20250103_01", f"{aid}_20250103_01"),
        ("daily", f"daily:{aid}_20250402_02", f"{aid}_20250402_02"),
    ]
    for gran, wkey, sid in cases:
        rep = generate_report(db, aid, gran, wkey, session_id=sid, view="coach", force=True)
        stem = wkey.replace(":", "_")
        (OUT / f"{gran}_{stem}_coach.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        (OUT / f"{gran}_{stem}_coach.md").write_text(_render_md(rep), encoding="utf-8")
        keys = [s["key"] for s in rep["sections"]]
        print(f"[{gran} {wkey}] 段={len(keys)} 建议={len(rep.get('suggestions', []))} keys={keys}")
    return 0


if __name__ == "__main__":
    sys.exit(main())