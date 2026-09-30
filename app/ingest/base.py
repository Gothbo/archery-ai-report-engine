# -*- coding: utf-8 -*-
"""接入层抽象：Source 接口 + ShotRaw / SessionRaw 数据契约。

统一逐箭记录结构，下游（事实层/指标层）与数据源解耦：
mock JSON 与真 SQLite 都产出本契约，M5 换数据源下游零改动。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol


@dataclass
class ShotRaw:
    """逐箭原始记录（对齐前，字段与 v1.2 接口对齐；缺测 NULL）。"""

    athlete_id: str
    session_id: str
    shot_seq: int
    score: float
    hit: bool
    x_mm: float | None = None
    y_mm: float | None = None
    mcr_t: float | None = None
    hr: int | None = None
    wind_speed: float | None = None
    wind_dir_deg: float | None = None
    shooting_mode: int = 1
    bow_type: str = ""
    video_ref: str | None = None
    shot_time_utc: str = ""


@dataclass
class SessionRaw:
    """一次训练的全部逐箭记录 + 维度摘要。"""

    session_id: str
    athlete_id: str
    shots: list[ShotRaw] = field(default_factory=list)
    distance_m: int = 70  # 反曲弓标准距离，真数据源覆盖
    site: str | None = None


class Source(Protocol):
    """数据源抽象：产出 SessionRaw 迭代器。"""

    def iter_sessions(self) -> Iterator[SessionRaw]:
        ...

    def athlete_profiles(self) -> dict[str, dict]:
        """{athlete_id: {name, ...}}：数据源自带的运动员档案（可选，默认空）。"""
        return {}
