# -*- coding: utf-8 -*-
"""API · 五档报告（D3 按钮触发：POST 生成 + GET 读取；?refresh=1 强制重生成）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.reports.generator import generate_report
from app.reports.window import WindowKey
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
    row = db.cached_report_by_id(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="报告不存在或已过期（同场次重新导入会失效）")
    return {"report_id": report_id, "status": "generated", "view": view}


@router.get("/athletes/{athlete_id}/reports")
def list_reports(athlete_id: str) -> dict:
    db = get_database()
    rows = db.cached_reports_of_athlete(athlete_id)
    return {"athlete_id": athlete_id, "reports": [dict(r) for r in rows]}


def _window_key(granularity: str, session_id: str | None, week: str | None,
                month: str | None, quarter: str | None, year: str | None) -> str:
    # 请求参数 → 存储窗口键：拼装/前缀契约由 WindowKey 统一承载（与 window_bounds 同源）
    if granularity == "daily":
        if not session_id:
            raise ValueError("daily 报告必须带 session_id")
        return WindowKey.daily(session_id).text
    provided = {"weekly": (week, "week", "2026-W36"),
                "monthly": (month, "month", "2026-08"),
                "quarterly": (quarter, "quarter", "2026Q3"),
                "yearly": (year, "year", "2026")}.get(granularity)
    if provided is None:
        raise ValueError("未知粒度")
    value, param, example = provided
    if not value:
        raise ValueError(f"{granularity} 报告必须带 {param}（如 {example}）")
    return WindowKey(granularity, value).text
