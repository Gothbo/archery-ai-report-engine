# -*- coding: utf-8 -*-
"""护栏（B1-4）：G1 禁语 / G2 数值一致 / G3 输出长度。

- G1：答案含配置禁用词 → 回退
- G2：答案所有数字必须能在结论骨架中找到（±1e-3），出现骨架外数字 → 回退
  （对运动员的承诺：对话里的环数/秒/毫米必须能在报告里找到；1.5B 编造 0.35s 实测教训）
- G3：答案长度超限 → 回退
"""
from __future__ import annotations

import re
from unicodedata import normalize

from app.rag.context import extract_numbers

_UNVERIFIED_QUANTITY = re.compile(
    r"[零〇一二两三四五六七八九十百千万亿半点]+\s*(?:成|环|秒|分钟|小时|毫米|厘米|米|次|支|箭|组|天|周|倍|%|个百分点)")
_MEDICAL = re.compile(
    r"服用|给药|用药|吃药|止痛药|布洛芬|阿司匹林|处方|剂量|诊断|手术|治疗|针灸|冰敷|热敷|康复训练|停药|就医|"
    r"\b(?:diagnose|prescri\w+|medication|dosage|treatment|surgery)\b", re.I)
_JUDGEMENT = re.compile(
    r"进步|退步|改善|回落|上升|下降|升高|降低|提升了|变差|变好|更稳|更差|恶化|平稳|波动显著|平台期|"
    r"有风险|高风险|超出阈值|未达标|(?<!不)达标|显著(?:变化|差异)|判定\s*[:：]|"
    r"\b(?:improv\w+|regress\w+|deteriorat\w+|plateau)\b", re.I)
_CLAUSE_END = re.compile(r"[。!?;\n]+")


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
    strict = context.get("strict_expression", False)
    text = normalize("NFKC", answer) if strict else answer
    hit = g1_forbidden(text, forbidden_phrases)
    if hit:
        return False, f"G1 命中禁用词：{hit}"
    bad = g2_numbers(text, context["numbers"], tol=0 if strict else 1e-3)
    if bad is not None:
        return False, f"G2 出现骨架外数字：{bad}"
    too_long = g3_length(answer, max_len)
    if too_long is not None:
        return False, f"G3 答案超长（{too_long} 字）"
    if strict:
        if _UNVERIFIED_QUANTITY.search(text):
            return False, "G2 出现无法核对的中文数量"
        if _MEDICAL.search(text):
            return False, "G1 训练指导不得给出医疗建议"
        approved = {part.strip() for line in context["judgement_lines"]
                    for part in _CLAUSE_END.split(normalize("NFKC", line))}
        if any(_JUDGEMENT.search(part) and part.strip() not in approved for part in _CLAUSE_END.split(text)):
            return False, "G1 判定必须引用报告原句"
    return True, None
