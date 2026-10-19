# -*- coding: utf-8 -*-
"""指标层 · MDC 指标向量（口径唯一来源）。

把「箭集 → 4 个 MDC 指标（avgScore / mcrT / hrVolatility / dispersionMm）」收敛为
单一实现：指标定义、缺测规则（单指标缺测存 NULL 而非 0，B11）、canonical 精度
都定义在此。报告层与记忆层都消费它，不再各自实现。

canonical 精度：avgScore 2dp / mcrT 3dp / hrVolatility 1dp / dispersionMm 1dp
（对齐报告展示口径；锚点与窗口同精度才可比）。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.metrics.performance import avg_score
from app.metrics.physiology import hr_volatility
from app.metrics.process import dispersion_mm, mean_mcr_t


@dataclass(frozen=True)
class MetricVector:
    """4 个 MDC 指标的值（canonical 精度；单指标缺测为 None）。"""

    avg_score: float | None
    mcr_t: float | None
    hr_volatility: float | None
    dispersion_mm: float | None


def metric_vector(shots: list[dict]) -> MetricVector:
    """箭集 → MDC 指标向量。"""
    scores = [s["score"] for s in shots]
    mcr_vals = [s["mcr_t"] for s in shots if s.get("mcr_t") is not None]
    hr_vals = [s["hr"] for s in shots if s.get("hr") is not None]
    pairs = [(s["x_mm"], s["y_mm"]) for s in shots
             if s.get("x_mm") is not None and s.get("y_mm") is not None]
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return MetricVector(
        avg_score=round(avg_score(scores), 2),
        mcr_t=round(mean_mcr_t(mcr_vals), 3) if mcr_vals else None,
        hr_volatility=round(hr_volatility(hr_vals), 1) if hr_vals else None,
        dispersion_mm=round(dispersion_mm(xs, ys), 1) if len(xs) >= 2 else None,
    )