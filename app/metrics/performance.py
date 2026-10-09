# -*- coding: utf-8 -*-
"""指标层 · 成绩指标（纯函数，可单测）。"""
from __future__ import annotations

from statistics import fmean


def avg_score(scores: list[float]) -> float:
    return fmean(scores) if scores else 0.0


def inner10_rate(scores: list[float]) -> float:
    """内十率（%）：环值 >= 10.0。"""
    return sum(1 for s in scores if s >= 10.0) / len(scores) * 100 if scores else 0.0


def far_miss_rate(scores: list[float], threshold: float = 9.0) -> float:
    """远弹率（%）：环值 < threshold（默认 9.0）。"""
    return sum(1 for s in scores if s < threshold) / len(scores) * 100 if scores else 0.0


def hit_rate(hits: list[bool]) -> float:
    return sum(1 for h in hits if h) / len(hits) * 100 if hits else 0.0


def total_score(scores: list[float]) -> float:
    return round(sum(scores), 1)
