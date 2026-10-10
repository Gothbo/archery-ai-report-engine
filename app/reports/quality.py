# -*- coding: utf-8 -*-
"""报告层 · 数据质量护栏（P0）：对不可能的分布与过小样本显式标注「数据存疑」。

纯函数：只读指标字典，输出存疑原因（供 data_integrity 段展示）；不写库、不判定。
两类存疑：
- 过小样本：窗口箭数低于描述性统计下限，统计量不可靠（仅标注）；
- 不可能分布：与指标定义相矛盾的结构性组合（标注并抑制方向判定）。

只断言「按指标定义必然成立」的不变量，避免误报：
- 远弹率 ≥ 脱靶率：0 环（脱靶）必然 < 9 环（远弹），脱靶 ⊆ 远弹；
- 内十率 + 远弹率 ≤ 100%：≥10 环与 <9 环互斥；
- 含脱靶均环 ∈ [0, score_max]，且 ≤ (1 − 脱靶率) × score_max（脱靶箭必为 0 环，拉低上限）。
不校验「命中率 + 脱靶率 = 100%」：本系统中 hit（命中）与 score（环值）是独立信号
（真实源由 score 派生 hit，mock 数据集二者独立），二者并不互补。

容差 0.2：比例指标在指标层已四舍五入到 1 位小数，用于吸收舍入误差，避免误报。
"""
from __future__ import annotations

from app.config import DataQuality

# 比例不变量容差（吸收指标层 1 位小数舍入）
_RATE_TOL = 0.2


def assess_quality(m: dict, dq: DataQuality) -> dict:
    """评估窗口指标的数据质量。

    返回 {suspect, block_judgement, reasons}：
    - suspect：是否存在任一存疑（过小样本或不可能分布）；
    - block_judgement：是否命中结构性矛盾（数据不可信，须抑制方向判定）；
    - reasons：存疑原因文本列表（调用方加「数据存疑：」前缀展示）。
    """
    reasons: list[str] = []

    # 空窗口：比例与样本下限在此无意义（0 箭的「命中率+脱靶率」恒为 0），
    # 且 data_integrity 已显式标注「空窗口」，此处不重复报存疑，避免误导。
    if m["n_shots"] == 0:
        return {"suspect": False, "block_judgement": False, "reasons": []}

    if m["n_shots"] < dq.min_shots_for_description:
        reasons.append(
            f"窗口箭数过小（n={m['n_shots']}，< {dq.min_shots_for_description}），统计量不可靠")

    contradictions: list[str] = []
    miss, far, inner = m["miss_rate"], m["far_miss_rate"], m["inner10_rate"]
    if far < miss - _RATE_TOL:
        contradictions.append(f"远弹率 {far:.1f}% 低于脱靶率 {miss:.1f}%（0 环箭必为远弹）")
    if inner + far > 100.0 + _RATE_TOL:
        contradictions.append(f"内十率 {inner:.1f}% 与远弹率 {far:.1f}% 之和超过 100%（两者互斥）")
    avg = m["avg_score"]
    if avg is not None:
        if not (0.0 <= avg <= dq.score_max):
            contradictions.append(f"含脱靶均环 {avg:.2f} 越界（应在 [0, {dq.score_max}]）")
        else:
            # 脱靶箭必为 0 环：非脱靶箭全取上限也到不了的上界，超出即矛盾
            upper = (1.0 - miss / 100.0) * dq.score_max
            if avg > upper + _RATE_TOL:
                contradictions.append(
                    f"含脱靶均环 {avg:.2f} 高于脱靶率 {miss:.1f}% 允许的上限 {upper:.2f}"
                    "（脱靶箭必为 0 环）")

    reasons.extend(contradictions)
    return {
        "suspect": bool(reasons),
        "block_judgement": bool(contradictions),
        "reasons": reasons,
    }