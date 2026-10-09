# -*- coding: utf-8 -*-
"""报告层 · 窗口切分（口径 A1：本地时区切窗；UTC 仅存储）。

- daily：单场（session_id）
- weekly：config.timezone 下的 ISO 周
- monthly：自然月 / quarterly：自然季 / yearly：自然年
- 窗口键语法（拼 / 拆 / 切片 / 字面量）统一由 WindowKey 值对象承载，避免多处各自表述
- mock 的 weekNo 是"每 3 场一批"人工切分，仅展示用；真数据一律按本模块切窗（A2）
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import EngineConfig
from app.timeutil import format_utc_ms, parse_iso_utc

GRANULARITIES = ("daily", "weekly", "monthly", "quarterly", "yearly")

_DAILY_PREFIX = "daily:"
_WEEKLY_RE = re.compile(r"^(\d{4})-W?(\d{1,2})$")
_MONTHLY_RE = re.compile(r"^(\d{4})-(\d{2})$")
_QUARTERLY_RE = re.compile(r"^(\d{4})Q(\d)$")
_YEARLY_RE = re.compile(r"^(\d{4})$")


def local_tz(cfg: EngineConfig) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def _iso_local_day(cfg: EngineConfig, iso_utc: str) -> datetime:
    """ISO UTC 串 → 本地时区的当日 00:00 datetime（用于窗口计算）。"""
    dt = parse_iso_utc(iso_utc)
    if dt is None:
        raise ValueError(f"无法解析时间：{iso_utc!r}")
    return dt.astimezone(local_tz(cfg)).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class WindowKey:
    """报告窗口键（值对象）：粒度 + 规范化键体，统一窗口键的拼/拆/切片/字面量。

    - text：存储键（daily 带 'daily:' 前缀，其余为裸键 2026-W32 / 2026-08 / 2026Q3 / 2026）
    - session_id：daily 的场次 id（其余 None），取代各处的前缀切片
    - bounds(cfg)：左闭右开 UTC 时间窗（daily 无时间窗 → ValueError）
    """

    granularity: str
    value: str

    @classmethod
    def daily(cls, session_id: str) -> "WindowKey":
        if not session_id:
            raise ValueError("daily 报告必须带 session_id")
        return cls("daily", session_id)

    @classmethod
    def parse(cls, granularity: str, key: str) -> "WindowKey":
        """从存储键解析（daily 兼容 'daily:<sid>' 前缀，其余为裸键）。"""
        if granularity == "daily":
            value = key[len(_DAILY_PREFIX):] if key.startswith(_DAILY_PREFIX) else key
            return cls.daily(value)
        return cls(granularity, key)

    @classmethod
    def of_shot(cls, cfg: EngineConfig, shot_time_utc: str, granularity: str) -> "WindowKey | None":
        """逐箭时间 → 所属窗口键（daily 或无归属粒度 → None）。"""
        if granularity == "daily":
            return None
        local = _iso_local_day(cfg, shot_time_utc)
        if granularity == "weekly":
            iso = local.isocalendar()
            return cls("weekly", f"{iso[0]}-W{iso[1]:02d}")
        if granularity == "monthly":
            return cls("monthly", f"{local.year}-{local.month:02d}")
        if granularity == "quarterly":
            return cls("quarterly", f"{local.year}Q{(local.month - 1) // 3 + 1}")
        if granularity == "yearly":
            return cls("yearly", str(local.year))
        return None

    @property
    def text(self) -> str:
        """存储键（report_cache.window_key）：daily 带前缀，其余裸键。"""
        return f"{_DAILY_PREFIX}{self.value}" if self.granularity == "daily" else self.value

    @property
    def session_id(self) -> str | None:
        """daily 的场次 id；其余粒度 None。"""
        return self.value if self.granularity == "daily" else None

    def bounds(self, cfg: EngineConfig) -> tuple[str, str]:
        """返回 (start_utc_iso, end_utc_iso) 左闭右开。"""
        tz = local_tz(cfg)
        if self.granularity == "weekly":
            year, week = self._match(_WEEKLY_RE, "weekly 窗口键格式应为 YYYY-Www（如 2026-W32）")
            start = _iso_week_start(year, week, tz)
            end = start + timedelta(weeks=1)
        elif self.granularity == "monthly":
            year, month = self._match(_MONTHLY_RE, "monthly 窗口键格式应为 YYYY-MM（如 2026-08）")
            start = datetime(year, month, 1, tzinfo=tz)
            end = start + timedelta(days=calendar.monthrange(year, month)[1])
        elif self.granularity == "quarterly":
            year, quarter = self._match(_QUARTERLY_RE, "quarterly 窗口键格式应为 YYYYQn（如 2026Q3）")
            month_start = (quarter - 1) * 3 + 1
            start = datetime(year, month_start, 1, tzinfo=tz)
            end_month = month_start + 3
            end = (datetime(year + 1, end_month - 12, 1, tzinfo=tz) if end_month > 12
                   else datetime(year, end_month, 1, tzinfo=tz))
        elif self.granularity == "yearly":
            (year,) = self._match(_YEARLY_RE, "yearly 窗口键格式应为 YYYY（如 2026）")
            start = datetime(year, 1, 1, tzinfo=tz)
            end = datetime(year + 1, 1, 1, tzinfo=tz)
        else:
            raise ValueError(
                f"daily 粒度不需要时间窗（用 session_id 定位），收到 window_key={self.value!r}")
        return format_utc_ms(start), format_utc_ms(end)

    def _match(self, pattern: re.Pattern, err: str) -> tuple[int, ...]:
        m = pattern.match(self.value)
        if not m:
            raise ValueError(err)
        return tuple(int(g) for g in m.groups())


def window_bounds(cfg: EngineConfig, granularity: str, window_key: str) -> tuple[str, str]:
    """返回 (start_utc_iso, end_utc_iso) 左闭右开。window_key 依粒度而定。"""
    return WindowKey.parse(granularity, window_key).bounds(cfg)


def _iso_week_start(year: int, week: int, tz) -> datetime:
    """ISO 周（周一为一周第一天）起始的本地日期。"""
    # 用第 1 个星期四所在的周为第 1 周（ISO 8601）
    jan4 = datetime(year, 1, 4, tzinfo=tz)
    start = jan4 - timedelta(days=jan4.weekday())  # 第 1 周的周一
    return start + timedelta(weeks=week - 1)


def window_of_shot(cfg: EngineConfig, shot_time_utc: str, granularity: str) -> str | None:
    """给定逐箭时间，返回所在窗口 key（用于验证切窗正确性）。"""
    wk = WindowKey.of_shot(cfg, shot_time_utc, granularity)
    return wk.text if wk is not None else None