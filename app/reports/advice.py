# -*- coding: utf-8 -*-
"""报告层 · 建议层（P0）：由判断信号生成确定性训练建议，每条可溯源。

纯函数：只读窗口指标、level 段结论键、风档均环、数据质量护栏结果，输出
{"suggestions": [str], "evidence": [dict]}；不写库、不判定、不推测原因。

设计原则（对齐 Jev 的显式判定 / 分层 / 审计轨迹）：
- 建议只陈述数据支持的结构与变化，带真实数值，不推测原因（装备/心态/训练量）；
- 不越界给医疗建议；
- 空窗口与数据存疑时给真实空状态或显式拒绝，不编造建议；
- 阈值来自 config.advice，为自有数据经验草案（试行，待教练校准），仅决定「是否提示」。

三类信号（按优先级）：
1. 成绩结构：脱靶 / 非脱靶远弹各自独立判阈值，可并列出现；内十率低仅在前两者均未触发时给出；
2. 风况适应：两档达标样本且档间均环差超阈值；
3. 趋势：基于 level 段方向判定（强断言 / 参考档分开表述）。

数据缺口（缺风速 / 心率）不在建议层重复提示——已在 data_integrity 段显式标注，避免同一事实两处出现。
"""
from __future__ import annotations

from app.config import EngineConfig
from app.metrics.environment import band_label


def _band_label(cfg: EngineConfig, idx: int) -> str:
    """风档索引 → 可读区间标签（与展示层共用 band_label，保证两处一致）。"""
    lo, hi = cfg.wind_bands[idx]
    return band_label(lo, hi)


def _trend(conclusion_keys: list[str]) -> tuple[str | None, bool]:
    """从结论键取趋势方向与是否强断言：('progress'|'regression', assertive)。"""
    if "progress" in conclusion_keys:
        return "progress", True
    if "regression" in conclusion_keys:
        return "regression", True
    if "progress_ref" in conclusion_keys:
        return "progress", False
    if "regression_ref" in conclusion_keys:
        return "regression", False
    return None, False


def build_advice(m: dict, *, conclusion_keys: list[str], wind_band_avg: dict[int, float],
                 wind_band_counts: dict[int, int], dq: dict, gate_ok: bool,
                 cfg: EngineConfig) -> dict:
    """生成窗口建议与溯源证据。

    返回 {"suggestions": [...], "evidence": [...]}；无数据 / 数据存疑时给出显式空状态或拒绝语。
    """
    # 空窗口：无数据不出建议（前端显示空状态，不编造）
    if m["n_shots"] == 0:
        return {"suggestions": [], "evidence": []}

    # 数据存疑（结构矛盾）：不可信数据不出建议，显式说明并留痕
    if dq["block_judgement"]:
        return {"suggestions": ["本窗口数据存疑（分布存在结构性矛盾），暂不给出训练建议"],
                "evidence": [{"type": "fact", "ref": "advice_blocked",
                              "value": {"reasons": dq["reasons"]}}]}

    a = cfg.advice
    suggestions: list[str] = []
    evidence: list[dict] = []

    if not gate_ok:
        suggestions.append("样本不足，以下建议仅供参考")

    miss, far, inner = m["miss_rate"], m["far_miss_rate"], m["inner10_rate"]
    avg, eff = m["avg_score"], m["effective_avg_score"]
    far_non_miss = round(far - miss, 1)

    # ① 成绩结构：脱靶与非脱靶远弹各自独立判阈值（可并列），内十率低仅在前两者均未触发时给出
    miss_high = miss >= a.miss_rate_high
    far_high = far_non_miss >= a.far_miss_high
    if miss_high:
        eff_txt = f"{eff:.2f}" if eff is not None else "—"
        # 含脱靶均环必 ≤ 有效箭均环；差值即脱靶造成的均环损失（正数）
        gap = round(eff - avg, 2) if (avg is not None and eff is not None) else None
        gap_txt = f"低 {gap:.2f} 环" if gap is not None else "差距显著"
        suggestions.append(
            f"脱靶率 {miss:.1f}%，含脱靶均环 {avg:.2f} 较有效箭均环 {eff_txt} {gap_txt}"
            "（脱靶造成的均环损失）；建议优先稳定撒放与命中")
        evidence.append({"type": "calc", "ref": "advice_miss",
                         "value": {"miss_rate": miss, "avg_score": avg,
                                   "effective_avg_score": eff, "gap": gap}})
    if far_high:
        suggestions.append(
            f"非脱靶远弹占比 {far_non_miss:.1f}%（远弹率 {far:.1f}% 已含脱靶），着点离散偏大；"
            "建议加强瞄准区控制与动作重复性")
        evidence.append({"type": "calc", "ref": "advice_far",
                         "value": {"far_miss_rate": far, "miss_rate": miss,
                                   "far_non_miss": far_non_miss}})
    if not miss_high and not far_high and inner <= a.inner10_low:
        suggestions.append(
            f"内十率 {inner:.1f}%，着点不够靠中心；建议微调瞄点/瞄区，提升中心命中")
        evidence.append({"type": "calc", "ref": "advice_inner",
                         "value": {"inner10_rate": inner, "far_miss_rate": far}})

    # ② 风况适应：仅取样本达标的档比较首末两档，避免小样本噪声
    bands = sorted((b, v) for b, v in wind_band_avg.items()
                   if wind_band_counts.get(b, 0) >= a.min_band_shots)
    if len(bands) >= 2:
        lo_b, lo_v = bands[0]
        hi_b, hi_v = bands[-1]
        delta = round(hi_v - lo_v, 2)
        if abs(delta) >= a.wind_band_delta:
            # 措辞随方向变化：高风档更低 = 风况适应问题（风感训练可改善）；
            # 低风档反而更低与风况影响方向相反，若仍套用风感训练建议会自相矛盾，改为提示核查
            if delta < 0:
                worse, remedy = hi_b, "建议增加风感与瞄准补偿训练"
            else:
                worse, remedy = lo_b, "与风况影响方向相反，建议核查该档样本"
            suggestions.append(
                f"风档 {_band_label(cfg, lo_b)} 与 {_band_label(cfg, hi_b)} 均环相差 "
                f"{abs(delta):.2f} 环（{_band_label(cfg, worse)} 更低）；{remedy}")
            evidence.append({"type": "fact", "ref": "advice_wind",
                             "value": {"band_avg": wind_band_avg,
                                       "band_counts": wind_band_counts, "delta": delta}})

    # ③ 趋势：基于 level 段方向判定；强断言与参考档分开表述，参考档不越界下结论
    direction, assertive = _trend(conclusion_keys)
    if direction == "progress":
        text = ("本窗口较锚点判定进步，建议保持当前训练安排并巩固" if assertive
                else "本窗口较锚点有进步方向提示（参考，未达强断言门槛），建议持续观察")
        suggestions.append(text)
        evidence.append({"type": "fact", "ref": "advice_progress",
                         "value": {"keys": conclusion_keys, "assertive": assertive}})
    elif direction == "regression":
        text = ("本窗口较锚点判定退步，建议关注近期训练负荷与状态" if assertive
                else "本窗口较锚点有退步方向提示（参考，未达强断言门槛），建议持续观察")
        suggestions.append(text)
        evidence.append({"type": "fact", "ref": "advice_regression",
                         "value": {"keys": conclusion_keys, "assertive": assertive}})

    # 数据缺口（缺风速 / 心率）不在此重复提示，见模块 docstring 与 data_integrity 段
    return {"suggestions": suggestions, "evidence": evidence}