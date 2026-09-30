# -*- coding: utf-8 -*-
"""API · 五档报告（D3 按钮触发：POST 生成 + GET 读取；?refresh=1 强制重生成）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.reports.generator import generate_report
from app.store.database import get_database

router = APIRouter(prefix="/api/v1", tags=["reports"])

GRANULARITIES = ("daily", "weekly", "monthly", "quarterly", "yearly")


@router.post("/athletes/{athlete_id}/reports/{granularity}")
def generate(athlete_id: str, granularity: str,
             session_id: str | None = None,
             week: str | None = None, month: str | None = None,
             quarter: str | None = None, year: str | None = None,
             view: str = Query(default="athlete", pattern="^(athlete|coach)$"),
             refresh: bool = False) -> dict:
    if granularity not in GRANULARITIES:
        raise HTTPException(status_code=400, detail=f"granularity 必须为 {GRANULARITIES}")
    try:
        window_key = _window_key(granularity, session_id, week, month, quarter, year)
        report = generate_report(get_database(), athlete_id, granularity, window_key,
                                 session_id=session_id if granularity == "daily" else None,
                                 view=view, force=refresh)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return report


@router.get("/reports/{report_id}")
def get_report(report_id: str,
               view: str = Query(default="athlete", pattern="^(athlete|coach)$")) -> dict:
    """读取已生成报告（按 report_id）。P1 报告缓存于内存，落盘版本见 M6 交付。"""
    db = get_database()
    rows = db.query("SELECT * FROM report_cache WHERE report_id=?", (report_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="报告不存在或已过期（同场次重新导入会失效）")
    return {"report_id": report_id, "status": "generated", "view": view}


@router.get("/athletes/{athlete_id}/reports")
def list_reports(athlete_id: str) -> dict:
    db = get_database()
    rows = db.query(
        "SELECT * FROM report_cache WHERE athlete_id=? ORDER BY generated_at_utc DESC", (athlete_id,))
    return {"athlete_id": athlete_id, "reports": [dict(r) for r in rows]}


def _window_key(granularity: str, session_id: str | None, week: str | None,
                month: str | None, quarter: str | None, year: str | None) -> str:
    # 窗口键契约：daily 带 "daily:" 前缀（缓存失效依赖 B10①）；其余粒度用裸键
    # （2026-W32 / 2026-08 / 2026Q3 / 2026），与 window_bounds/测试口径一致
    if granularity == "daily":
        if not session_id:
            raise ValueError("daily 报告必须带 session_id")
        return f"daily:{session_id}"
    if granularity == "weekly":
        if not week:
            raise ValueError("weekly 报告必须带 week（如 2026-W36）")
        return week
    if granularity == "monthly":
        if not month:
            raise ValueError("monthly 报告必须带 month（如 2026-08）")
        return month
    if granularity == "quarterly":
        if not quarter:
            raise ValueError("quarterly 报告必须带 quarter（如 2026Q3）")
        return quarter
    if granularity == "yearly":
        if not year:
            raise ValueError("yearly 报告必须带 year（如 2026）")
        return year
    raise ValueError("未知粒度")
