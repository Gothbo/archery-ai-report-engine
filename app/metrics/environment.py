# -*- coding: utf-8 -*-
"""指标层 · 环境/风况指标（纯函数，可单测）。

风档为左闭右开 [lo, hi)；连续性由 config 校验。
缺测（None / 负值哨兵 / NaN）→ wind_band_of 返回 None，调用方跳过（接口 v1.2）。
bands 可注入（测试 / 纯函数调用）；默认读 config.wind_bands。
"""
from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean

from app.config import get_config


def wind_band_of(wind_speed: float | None,
                 bands: Sequence[Sequence[float]] | None = None) -> int | None:
    """返回风档索引。

    - 缺测（None / 负值哨兵如 -1 / NaN）→ 返回 None，调用方跳过（接口 v1.2 §十）
    - 正值不在任何档 → 抛 ValueError
    """
    if wind_speed is None or wind_speed != wind_speed or wind_speed < 0:
        return None
    if bands is None:
        bands = get_config().wind_bands
    for i, (lo, hi) in enumerate(bands):
        if lo <= wind_speed < hi:
            return i
    raise ValueError(f"风速 {wind_speed} 不在任何风档（左闭右开）")


def wind_band_avg_scores(shots: list[dict],
                         bands: Sequence[Sequence[float]] | None = None,
                         band_index: int | None = None) -> dict[int, float]:
    """按风档统计均环：{band_index: avg_score}；band_index=None 返回全部档。"""
    if bands is None:
        bands = get_config().wind_bands
    grouped: dict[int, list[float]] = {}
    for sh in shots:
        ws = sh.get("wind_speed")
        if ws is None:
            continue
        b = wind_band_of(ws, bands)
        if b is None or (band_index is not None and b != band_index):
            continue
        grouped.setdefault(b, []).append(sh["score"])
    return {b: round(fmean(v), 3) for b, v in grouped.items() if v}


def wind_band_counts(shots: list[dict],
                     bands: Sequence[Sequence[float]] | None = None) -> dict[int, int]:
    """每档箭数（样本门槛核验用）。"""
    if bands is None:
        bands = get_config().wind_bands
    counts: dict[int, int] = {}
    for sh in shots:
        ws = sh.get("wind_speed")
        if ws is None:
            continue
        b = wind_band_of(ws, bands)
        if b is None:
            continue
        counts[b] = counts.get(b, 0) + 1
    return counts


def wind_gap(wind_avg: float, no_wind_avg: float) -> float:
    """风对成绩影响：高风均环 - 低风均环（负值 = 风拖低成绩）。"""
    return round(wind_avg - no_wind_avg, 3)


def band_label(lo: float, hi: float) -> str:
    """风档区间 → 可读标签（如 [0.0,1.5)；末档 hi>=99 记为 ≥lo）。"""
    return f"≥{lo:.1f}" if hi >= 99 else f"[{lo:.1f},{hi:.1f})"
