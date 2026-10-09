# -*- coding: utf-8 -*-
"""指标层 · 生理指标（纯函数，可单测）：hrVolatility（跨箭心率波动，bpm）。

口径修订 C2：原 RMSSD 量纲错误（对 bpm 序列做差分 RMS 却标 ms），改名 hrVolatility，
单位 bpm，方向 +1→-1（波动越大越差）；MDC 阈值待专家共识在 bpm 尺度重定（M4.5）。
"""
from __future__ import annotations

import math


def hr_volatility(hr_list: list[int]) -> float:
    """跨箭心率波动指数（bpm）：相邻箭心率差值的 RMS。"""
    if len(hr_list) < 3:
        return 0.0
    diffs = [hr_list[i + 1] - hr_list[i] for i in range(len(hr_list) - 1)]
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))
