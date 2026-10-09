# -*- coding: utf-8 -*-
"""指标层 · 过程指标（纯函数，可单测）：MCRT / 散布 / 偏移。"""
from __future__ import annotations

import math
from statistics import fmean, pstdev


def mean_mcr_t(mcr_vals: list[float]) -> float:
    """撒放用时均值（秒）。"""
    return round(fmean(mcr_vals), 3) if mcr_vals else 0.0


def dispersion_mm(xs: list[float], ys: list[float]) -> float:
    """散布（毫米）：x/y 两轴标准差平方均值开方（与 MVP report_engine 同口径）。"""
    if len(xs) < 2 or len(ys) < 2:
        return 0.0
    pooled = math.sqrt((pstdev(xs) ** 2 + pstdev(ys) ** 2) / 2)
    return pooled


def offset_mm(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """弹着中心偏移（毫米）：均值坐标。"""
    return (fmean(xs) if xs else 0.0, fmean(ys) if ys else 0.0)
