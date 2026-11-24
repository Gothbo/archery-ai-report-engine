# -*- coding: utf-8 -*-
"""Mock 应答器（provider=mock）：从结论骨架抽取数值组句，G2 恒通过（演示/测试用）。

只允许使用骨架中出现的数字：正则抽取自 skeleton_json，组句文本不含骨架外数值。
"""
from __future__ import annotations

import json
import re

_AVG_RE = re.compile(r"(含脱靶均环|平均环)\s*(\d+(?:\.\d+)?)")
_EFFECTIVE_AVG_RE = re.compile(r"有效箭均环\s*(\d+(?:\.\d+)?)")
_N_RE = re.compile(r"n=(\d+)")
_INNER10_RE = re.compile(r"内十率\s*([\d.]+)%")
_FAR_MISS_RE = re.compile(r"远弹率\s*([\d.]+)%")


def demo_responder(messages: list[dict]) -> str:
    """根据系统消息中的骨架生成固定结构解读（数字全部来自骨架）。"""
    skeleton_json = ""
    for m in messages:
        if m.get("role") == "system" and "skeleton_json" in m.get("content", ""):
            skeleton_json = m["content"]
    if not skeleton_json:
        return "（mock：骨架缺失）"
    try:
        payload = json.loads(skeleton_json.split("skeleton_json: ", 1)[1].split("\nuser:", 1)[0])
    except Exception:
        return "（mock：骨架解析失败）"

    sections = {section["key"]: section for section in payload.get("sections", [])}
    score = sections.get("window_score", {})
    text = "；".join(score.get("content", []))
    period = score.get("title", "").removesuffix("成绩") or "本期"
    avg = _AVG_RE.search(text)
    average_sentence = ""
    if avg:
        details = ["含脱靶"] if avg.group(1) == "含脱靶均环" else []
        n = _N_RE.search(text)
        if n:
            details.append(f"n={n.group(1)}")
        suffix = f"（{'，'.join(details)}）" if details else ""
        average_sentence = f"{period}平均环 {avg.group(2)} 环{suffix}。"

    is_guidance = "【训练指导】" in skeleton_json
    question = next((m["content"] for m in messages if m.get("role") == "user"), "")
    if not is_guidance and re.search(r"均环|平均环", question) and re.search(r"多少|几环", question):
        if re.search(r"有效箭|不含脱靶", question):
            effective = _EFFECTIVE_AVG_RE.search(text)
            return (f"{period}有效箭均环 {effective.group(1)} 环。" if effective
                    else "报告没有可用的有效箭均环。")
        return average_sentence or "报告没有可用的均环。"

    parts = [average_sentence] if average_sentence else []
    # 沿用报告的判定原句，避免把方向提示或描述性指标升级为进步判定。
    for line in sections.get("level", {}).get("content", []):
        if line.endswith(("判定：进步", "判定：退步")):
            parts.append(line + "。")
    inner10 = _INNER10_RE.search(text)
    far_miss = _FAR_MISS_RE.search(text)
    if inner10:
        tail = f"内十率 {inner10.group(1)}%"
        if far_miss:
            tail += f"，远弹率 {far_miss.group(1)}%"
        parts.append(tail + "。")

    answer = "".join(parts)
    if is_guidance:
        return _demo_guidance(answer)
    return answer or "（mock：骨架无可解读内容）"


def _demo_guidance(answer: str) -> str:
    """训练指导演示文本（PR #3）：固定五个小标题；数字只来自上面骨架取数的 answer。"""
    return "\n".join([
        "一、本次概述",
        answer or "报告未给出可解读的变化。",
        "二、数据基础",
        "以本期报告的有效箭数、锚点与滚动基线为依据；报告未包含的数据不做推断。",
        "三、技术表现",
        "以报告中的环值、散布与撒放节奏结论为准，保持当前动作一致性。",
        "四、状态与负荷",
        "报告未包含该数据。",
        "五、提升方案",
        "按报告建议逐项落实，保持训练节奏；涉及伤病与训练调整以备注为准。",
    ])
