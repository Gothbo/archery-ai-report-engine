# -*- coding: utf-8 -*-
"""报告层 · 窗口切分（口径 A1：本地时区切窗；UTC 仅存储）。

- daily：单场（session_id）
- weekly：config.timezone 下的 ISO 周
- monthly：自然月 / quarterly：自然季 / yearly：自然年
- mock 的 weekNo 是"每 3 场一批"人工切分，仅展示用；真数据一律按本模块切窗（A2）
"""
from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import EngineConfig

GRANULARITIES = ("daily", "weekly", "monthly", "quarterly", "yearly")


def local_tz(cfg: EngineConfig) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def _to_utc_iso(dt_local: datetime) -> str:
    """本地时间 → UTC ISO 毫秒（YYYY-MM-DDTHH:MM:SS.mmmZ）。"""
    utc = dt_local.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _iso_local_day(cfg: EngineConfig, iso_utc: str) -> datetime:
    """ISO UTC 字符串 → 本地时区的当日 00:00 datetime（用于窗口计算）。"""
    norm = iso_utc.replace("Z", "+00:00") if iso_utc.endswith(("Z", "z")) else iso_utc
    dt = datetime.fromisoformat(norm)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(local_tz(cfg))
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def window_bounds(cfg: EngineConfig, granularity: str, window_key: str) -> tuple[str, str]:
    """返回 (start_utc_iso, end_utc_iso) 左闭右开。window_key 依粒度而定。"""
    tz = local_tz(cfg)
    if granularity == "weekly":
        year_s, week_s = window_key.split("-W") if "-W" in window_key else window_key.split("-")
        year, week = int(year_s), int(week_s)
        start = _iso_week_start(year, week, tz)  # ISO 周：周一为起始
        end = start + timedelta(weeks=1)
        return _to_utc_iso(start), _to_utc_iso(end)
    if granularity == "monthly":
        year_s, month_s = window_key.split("-")
        year, month = int(year_s), int(month_s)
        start = datetime(year, month, 1, tzinfo=tz)
        end = start + timedelta(days=calendar.monthrange(year, month)[1])
        return _to_utc_iso(start), _to_utc_iso(end)
    if granularity == "quarterly":
        parts = window_key.split("Q")
        year = int(parts[0])
        quarter = int(parts[1])
        month_start = (quarter - 1) * 3 + 1
        start = datetime(year, month_start, 1, tzinfo=tz)
        end_month = month_start + 3
        if end_month > 12:
            end = datetime(year + 1, end_month - 12, 1, tzinfo=tz)
        else:
            end = datetime(year, end_month, 1, tzinfo=tz)
        return _to_utc_iso(start), _to_utc_iso(end)
    if granularity == "yearly":
        year = int(window_key)
        start = datetime(year, 1, 1, tzinfo=tz)
        end = datetime(year + 1, 1, 1, tzinfo=tz)
        return _to_utc_iso(start), _to_utc_iso(end)
    raise ValueError(f"daily 粒度不需要时间窗（用 session_id 定位），收到 window_key={window_key!r}")


def _iso_week_start(year: int, week: int, tz) -> datetime:
    """ISO 周（周一为一周第一天）起始的本地日期。"""
    # 用第 1 个星期四所在的周为第 1 周（ISO 8601）
    jan4 = datetime(year, 1, 4, tzinfo=tz)
    start = jan4 - timedelta(days=jan4.weekday())  # 第 1 周的周一
    return start + timedelta(weeks=week - 1)


def window_of_shot(cfg: EngineConfig, shot_time_utc: str, granularity: str) -> str | None:
    """给定逐箭时间，返回所在窗口 key（用于验证切窗正确性）。"""
    local = _iso_local_day(cfg, shot_time_utc)
    if granularity == "weekly":
        iso = local.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    if granularity == "monthly":
        return f"{local.year}-{local.month:02d}"
    if granularity == "quarterly":
        return f"{local.year}Q{(local.month - 1) // 3 + 1}"
    if granularity == "yearly":
        return str(local.year)
    return None
