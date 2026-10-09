# -*- coding: utf-8 -*-
"""Mock 应答器（provider=mock）：从结论骨架抽取数值组句，G2 恒通过（演示/测试用）。

只允许使用骨架中出现的数字：正则抽取自 skeleton_json，组句文本不含骨架外数值。
"""
from __future__ import annotations

import json
import re

_AVG_RE = re.compile(r"平均环\s*([\d.]+)")
_N_RE = re.compile(r"n=(\d+)")
_PROGRESS_RE = re.compile(r"进步\s*([\d.]+)\s*环")
_REGRESS_RE = re.compile(r"退步\s*([\d.]+)\s*环")
_SHORTEN_RE = re.compile(r"缩短\s*([\d.]+)\s*s\b")
_NARROW_RE = re.compile(r"收窄\s*([\d.]+)\s*mm\b")
_INNER10_RE = re.compile(r"内十率\s*([\d.]+)%")
_FAR_MISS_RE = re.compile(r"远弹率\s*([\d.]+)%")
_DATE_RE = re.compile(r"锚点（([\d-]+)\s*采集）")


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

    text = "\n".join(
        f"[{s['title']}] {'；'.join(str(c) for c in s['content'])}"
        for s in payload.get("sections", [])
    )
    parts: list[str] = []

    avg = _AVG_RE.search(text)
    n = _N_RE.search(text)
    if avg:
        head = f"本周平均环 {avg.group(1)}"
        if n:
            head += f"（n={n.group(1)}）"
        parts.append(head + "。")

    date = _DATE_RE.search(text)
    progress = _PROGRESS_RE.search(text)
    regress = _REGRESS_RE.search(text)
    if progress:
        parts.append(f"较入队测试锚点（{date.group(1)} 采集）进步 {progress.group(1)} 环，超过最小可检测变化，进步判定成立。")
    elif regress:
        parts.append(f"较入队测试锚点（{date.group(1)} 采集）退步 {regress.group(1)} 环，需关注。")

    shorten = _SHORTEN_RE.search(text)
    if shorten:
        parts.append(f"撒放用时较锚点缩短 {shorten.group(1)} 秒，节奏稳定。")
    narrow = _NARROW_RE.search(text)
    if narrow:
        parts.append(f"散布较锚点收窄 {narrow.group(1)} 毫米，动作一致性改善。")

    inner10 = _INNER10_RE.search(text)
    far_miss = _FAR_MISS_RE.search(text)
    if inner10:
        tail = f"内十率 {inner10.group(1)}%"
        if far_miss:
            tail += f"，远弹率 {far_miss.group(1)}%"
        parts.append(tail + "。")

    answer = "进步了。" + "".join(parts) if parts and not regress else "".join(parts)
    return answer or "（mock：骨架无可解读内容）"
