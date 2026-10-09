# -*- coding: utf-8 -*-
"""记忆层 · 历史结论沉淀（报告生成时自动写入，供历史引用）。

- judgement 类 {progress, regression, plateau, risk} 写入 report_memories 供历史引用
  （含 delta_value 供锚点重建触发④使用）
- attribution 类 {wind} 仅随报告落库，不参与历史结论检索（C7）
- 引用规则（B2/D5）：同粒度、时间最近 1 条（generated_at_utc DESC）
"""
from __future__ import annotations

import logging

from app.store.database import Database
from app.timeutil import iso_now_utc

logger = logging.getLogger("engine.memory.memories")

# 参与历史引用的判定类：progress/regression/plateau/steady/risk；steady 落库供平台期识别（C3）
JUDGEMENT_KEYS = {"progress", "regression", "plateau", "steady", "risk"}
ATTRIBUTION_KEYS = {"wind"}


def record_conclusion(db: Database, *, athlete_id: str, report_id: str, granularity: str,
                      conclusion_key: str, conclusion: str, judge_basis: str | None,
                      delta_value: float | None, evidence: str | None) -> int | None:
    """写一条结论沉淀。返回记忆 id；attribution 类同样落库（不参与引用）。"""
    if conclusion_key in JUDGEMENT_KEYS:
        ctype = "judgement"
    elif conclusion_key in ATTRIBUTION_KEYS:
        ctype = "attribution"
    else:
        logger.warning("未知 conclusion_key=%s，跳过沉淀", conclusion_key)
        return None
    mem = {
        "athlete_id": athlete_id,
        "report_id": report_id,
        "granularity": granularity,
        "conclusion_type": ctype,
        "conclusion_key": conclusion_key,
        "conclusion": conclusion,
        "judge_basis": judge_basis,
        "delta_value": delta_value,
        "evidence": evidence,
        "generated_at_utc": iso_now_utc(),
    }
    mid = db.insert_report_memory(mem)
    logger.info("结论沉淀 id=%d athlete=%s key=%s", mid, athlete_id, conclusion_key)
    return mid


def latest_judgement(db: Database, athlete_id: str, granularity: str) -> dict | None:
    """历史结论引用：同粒度、时间最近 1 条 judgement（B2/D5）。"""
    row = db.latest_memory(athlete_id, granularity)
    return dict(row) if row else None
