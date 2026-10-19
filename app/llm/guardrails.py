# -*- coding: utf-8 -*-
"""护栏（B1-4）：G1 禁语 / G2 数值一致 / G3 输出长度。

- G1：答案含配置禁用词 → 回退
- G2：答案所有数字必须能在结论骨架中找到（±1e-3），出现骨架外数字 → 回退
  （对运动员的承诺：对话里的环数/秒/毫米必须能在报告里找到；1.5B 编造 0.35s 实测教训）
- G3：答案长度超限 → 回退
"""
from __future__ import annotations

from app.rag.context import extract_numbers


def g1_forbidden(answer: str, forbidden_phrases: list[str]) -> str | None:
    for phrase in forbidden_phrases:
        if phrase and phrase in answer:
            return phrase
    return None


def g2_numbers(answer: str, skeleton_numbers: list[float], tol: float = 1e-3) -> float | None:
    """返回第一个骨架外数字；全通过返回 None。"""
    known = skeleton_numbers
    for n in extract_numbers(answer):
        if not any(abs(n - s) <= tol for s in known):
            return n
    return None


def g3_length(answer: str, max_len: int = 500) -> int | None:
    return len(answer) if len(answer) > max_len else None


def check_guards(answer: str, context: dict, forbidden_phrases: list[str],
                 max_len: int = 500) -> tuple[bool, str | None]:
    """综合检查：返回 (是否通过, 失败原因)。max_len 默认 500（/ask）；训练指导传 llm.guidance_max_len。"""
    if not answer.strip():
        return False, "答案为空"
    hit = g1_forbidden(answer, forbidden_phrases)
    if hit:
        return False, f"G1 命中禁用词：{hit}"
    bad = g2_numbers(answer, context["numbers"])
    if bad is not None:
        return False, f"G2 出现骨架外数字：{bad}"
    too_long = g3_length(answer, max_len)
    if too_long is not None:
        return False, f"G3 答案超长（{too_long} 字）"
    return True, None
