# -*- coding: utf-8 -*-
"""报告层 · 规则引擎智能指导（确定性骨架）。

纯函数：只读已有 report 字段（suggestions / advice_evidence / metrics /
conclusions），组装面向 idle / 「只看规则建议」/ COACH-OFFLINE 的结构化指导。
不新建规则体系、不调用 LLM、不编造空窗口指导。

与 advice.build_advice 同源：训练建议正文仍由 suggestions 给出；本模块只做
包装与空状态，并在 conclusions.plateau 触发时补一条与正文一致的平台期提示。
"""
from __future__ import annotations

TITLE = "规则引擎智能指导"

# 与 generator 平台期正文同口径（结论已触发时复用，不另起阈值）
_PLATEAU_TEXT = (
    "近 {n} 期成绩均与锚点平稳（差值未超最小可检测变化），疑似进入平台期，"
    "建议调整训练刺激（辅助判断，仅供参考）"
)

_BLOCKED_MARK = "数据存疑"
_CAVEAT_PREFIX = "样本不足"


def build_rule_guidance(
    *,
    suggestions: list[str] | None,
    advice_evidence: list[dict] | None = None,
    metrics: dict | None = None,
    conclusions: dict | None = None,
) -> dict:
    """组装确定性规则指导。

    返回 {"title", "summary", "bullets":[{"text","source"}], "empty_reason"?}。
    empty_reason：no_shots / data_suspect / no_signals；有有效子弹时为 None。
    """
    sug = list(suggestions or [])
    evidence = list(advice_evidence or [])
    m = metrics or {}
    conclusions = conclusions or {}
    n_shots = int(m.get("n_shots") or 0)

    if n_shots == 0:
        return {
            "title": TITLE,
            "summary": "本窗口无箭，暂无规则指导",
            "bullets": [],
            "empty_reason": "no_shots",
        }

    if sug and any(_BLOCKED_MARK in s for s in sug):
        bullets = _pair_bullets(sug, evidence)
        return {
            "title": TITLE,
            "summary": "本窗口数据存疑，暂不给出训练建议",
            "bullets": bullets,
            "empty_reason": "data_suspect",
        }

    bullets = _pair_bullets(sug, evidence)
    _maybe_append_plateau(bullets, conclusions.get("plateau"))

    if not bullets:
        return {
            "title": TITLE,
            "summary": _metrics_summary(m) + "本窗口指标未触发规则建议阈值，暂无额外指导",
            "bullets": [],
            "empty_reason": "no_signals",
        }

    return {
        "title": TITLE,
        "summary": _metrics_summary(m) + f"共 {len(bullets)} 条规则建议（可溯源）",
        "bullets": bullets,
        "empty_reason": None,
    }


def _pair_bullets(suggestions: list[str], evidence: list[dict]) -> list[dict]:
    """建议正文与 advice_evidence 按生成顺序配对；声明类无 evidence 时给固定 source。"""
    bullets: list[dict] = []
    ev_i = 0
    for text in suggestions:
        if text.startswith(_CAVEAT_PREFIX):
            bullets.append({"text": text, "source": "caveat_sample"})
            continue
        if _BLOCKED_MARK in text:
            ref = evidence[0]["ref"] if evidence else "advice_blocked"
            bullets.append({"text": text, "source": ref})
            continue
        if ev_i < len(evidence):
            source = evidence[ev_i].get("ref") or "advice"
            ev_i += 1
        else:
            source = "advice"
        bullets.append({"text": text, "source": source})
    return bullets


def _maybe_append_plateau(bullets: list[dict], plateau) -> None:
    """平台期已触发且尚未出现在建议中时，补一条与正文同口径的提示。"""
    if not isinstance(plateau, dict) or not plateau.get("triggered"):
        return
    n = plateau.get("periods")
    if not n:
        return
    text = _PLATEAU_TEXT.format(n=n)
    if any(text == b["text"] or "平台期" in b["text"] for b in bullets):
        return
    bullets.append({"text": text, "source": "plateau"})


def _metrics_summary(m: dict) -> str:
    """短指标摘要，只用已有数字，不编造。"""
    parts: list[str] = []
    n = m.get("n_shots")
    if n is not None:
        parts.append(f"箭数 {n}")
    avg = m.get("avg_score")
    if avg is not None:
        parts.append(f"均环 {avg:.2f}" if isinstance(avg, (int, float)) else f"均环 {avg}")
    miss = m.get("miss_rate")
    if miss is not None:
        parts.append(f"脱靶率 {miss:.1f}%" if isinstance(miss, (int, float)) else f"脱靶率 {miss}%")
    if not parts:
        return ""
    return "；".join(parts) + "。"
