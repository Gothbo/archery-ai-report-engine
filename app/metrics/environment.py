# -*- coding: utf-8 -*-
"""指标层 · 环境归因（纯函数，可单测）：风档分层 + 风对成绩影响。

口径 C4/A5：趋势判定不用低风/高风二分，改风档分层（左闭右开 lo<=w<hi，
边界进 config wind_bands）；优先取 [0,1.5) m/s 档均环做跨周趋势。
"""
from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean


def wind_band_of(wind_speed: float, bands: Sequence[Sequence[float]]) -> int:
    """返回风档索引；不在任何档 → 抛 ValueError（左闭右开连续性由 config 校验保证）。

    风档由调用方注入（config.wind_bands），本函数为纯函数、不读全局配置。
    """
    for i, (lo, hi) in enumerate(bands):
        if lo <= wind_speed < hi:
            return i
    raise ValueError(f"风速 {wind_speed} 不在任何风档（左闭右开）")


def wind_band_avg_scores(shots: list[dict], bands: Sequence[Sequence[float]],
                         band_index: int | None = None) -> dict[int, float]:
    """按风档统计均环：{band_index: avg_score}；band_index=None 返回全部档。"""
    grouped: dict[int, list[float]] = {}
    for sh in shots:
        ws = sh.get("wind_speed")
        if ws is None:
            continue
        b = wind_band_of(ws, bands)
        if band_index is not None and b != band_index:
            continue
        grouped.setdefault(b, []).append(sh["score"])
    return {b: round(fmean(v), 3) for b, v in grouped.items() if v}


def wind_band_counts(shots: list[dict], bands: Sequence[Sequence[float]]) -> dict[int, int]:
    """每档箭数（样本门槛核验用）。"""
    counts: dict[int, int] = {}
    for sh in shots:
        ws = sh.get("wind_speed")
        if ws is None:
            continue
        b = wind_band_of(ws, bands)
        counts[b] = counts.get(b, 0) + 1
    return counts


def wind_gap(wind_avg: float, no_wind_avg: float) -> float:
    """风对成绩影响：高风均环 - 低风均环（负值 = 风拖低成绩）。"""
    return round(wind_avg - no_wind_avg, 3)
