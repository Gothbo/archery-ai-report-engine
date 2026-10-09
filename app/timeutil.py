# -*- coding: utf-8 -*-
"""时间工具：UTC ISO 毫秒口径的唯一实现。

存储一律 UTC ISO 毫秒（YYYY-MM-DDTHH:MM:SS.mmmZ），本地时区仅用于切窗（A1）。
集中「当前时刻 / datetime→串 / 串→datetime / 串→epoch 毫秒」，避免多处重复实现漂移。
"""
from __future__ import annotations

from datetime import datetime, timezone


def format_utc_ms(dt: datetime) -> str:
    """datetime → UTC ISO 毫秒串；naive 视为 UTC，其余先转 UTC。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_now_utc() -> str:
    """当前 UTC 时刻 → ISO 毫秒串。"""
    return format_utc_ms(datetime.now(timezone.utc))


def parse_iso_utc(iso: str) -> datetime | None:
    """ISO 串 → 带 UTC 时区的 datetime；非法/空返回 None。naive 视为 UTC。"""
    if not iso:
        return None
    norm = iso.replace("Z", "+00:00") if iso.endswith(("Z", "z")) else iso
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def parse_iso_utc_ms(iso: str) -> int | None:
    """ISO 串 → epoch 毫秒；非法/空返回 None。"""
    dt = parse_iso_utc(iso)
    return int(dt.timestamp() * 1000) if dt is not None else None