# -*- coding: utf-8 -*-
"""上下文组装器（B1-3）：确定性检索（结构化优先，不引入向量）。

数据源与报告生成器同源，保证「对话里的数字能在报告里找到」（G2 承诺）：
- 档案 athlete_profile
- 目标窗口报告 sections（指定窗口则生成该窗口；未指定取最近一份已生成报告）
- 历史结论（同粒度最近 1 条 judgement，沿用记忆契约 B2/D5）
- active 备注（14 天窗口，双视角过滤 A2/D9）
- 滚动基线 coach_extra.rolling_baseline

输出：skeleton（结构化结论骨架 + 全部数值）、sources（溯源列表）、
numbers（骨架全部数字，供 G2）、version（上下文版本键）、
suggested_note（可提炼建议，MVP 只展示不落库 D4）。

预留：retrieve_semantic() 为自由文本 RAG 接入点（bge-m3 储备，P2.3d），本轮不实现。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Callable

from app.config import get_config
from app.memory.notes import list_notes_for_view
from app.reports.generator import generate_report
from app.store.database import Database

logger = logging.getLogger("engine.rag.context")

GRANULARITIES = ("daily", "weekly", "monthly", "quarterly", "yearly")

_NOTE_KEYWORDS = ("伤", "疼", "痛", "目标", "建议", "注意", "调整", "恢复", "改进")

# 预留：自由文本语义检索接入点（P2.3d，bge-m3 储备）。本轮不实现，保持确定性检索优先。
_retrieve_semantic: Callable[..., list[dict]] | None = None


def assemble_context(db: Database, athlete_id: str, question: str,
                     granularity: str | None = None, window_key: str | None = None,
                     view: str = "athlete") -> dict:
    """组装问询上下文。窗口缺失时取最近一份已生成报告（report_cache 最新一条）。"""
    cfg = get_config()
    profile = db.get_profile(athlete_id)
    if profile is None:
        raise ValueError(f"运动员 {athlete_id} 不存在")

    if granularity is not None:
        if granularity not in GRANULARITIES:
            raise ValueError(f"granularity 必须为 {GRANULARITIES}")
        if window_key is None:
            raise ValueError("指定 granularity 时必须同时指定 window_key")
        _granularity, _window_key = granularity, window_key
    else:
        # 未指定 → 最近一份已生成报告（先到先得，未生成则报错引导先生成）
        latest = db.query(
            "SELECT granularity, window_key FROM report_cache WHERE athlete_id=? "
            "ORDER BY generated_at_utc DESC LIMIT 1", (athlete_id,))
        if not latest:
            raise ValueError("运动员暂无已生成报告：请先在报告区生成一份，再发起对话")
        _granularity, _window_key = latest[0]["granularity"], latest[0]["window_key"]

    # PR #4：有已存正文就直接用（report_id / 生成时间 / 历史结论都不变）；以前这里 force=True，
    # 是因为缓存命中只给 id、拿不到正文 —— 每问一次报告就重算、换新 id。
    # 没有缓存 → 正常生成；PR #4 之前的旧缓存行（无正文）→ generate_report 自己补存正文（report_id 与历史结论不变）。
    # 下面的 force=True 只是兜底（正常走不到）。
    session_id = _window_key[6:] if _granularity == "daily" else None
    report = generate_report(db, athlete_id, _granularity, _window_key,
                             session_id=session_id, view=view, force=False)
    if "sections" not in report:
        report = generate_report(db, athlete_id, _granularity, _window_key,
                                 session_id=session_id, view=view, force=True)

    notes = list_notes_for_view(db, athlete_id, view)
    history = db.latest_memory(athlete_id, _granularity)

    skeleton_text = _skeleton_text(report, notes, history)
    sources = _sources(report, notes, history)
    numbers = extract_numbers(skeleton_text)
    version = hashlib.sha1(skeleton_text.encode("utf-8")).hexdigest()[:12]

    return {
        "athlete_id": athlete_id,
        "question": question,
        "granularity": _granularity,
        "window_key": _window_key,
        "report_id": report.get("report_id"),
        "skeleton": skeleton_text,
        "skeleton_json": _skeleton_json(report, notes, history),
        "numbers": numbers,
        "sources": sources,
        "version": version,
        "suggested_note": _suggested_note(question, notes),
    }


def _skeleton_text(report: dict, notes: list[dict], history) -> str:
    lines: list[str] = []
    for sec in report["sections"]:
        lines.append(f"[{sec['title']}] " + "；".join(str(c) for c in sec["content"]))
    rb = report.get("coach_extra", {}).get("rolling_baseline")
    if rb:
        lines.append(f"[滚动基线] n={rb['n_shots']} 平均环={rb.get('avg_score')}")
    for n in notes:
        lines.append(f"[备注] {n['content']}（类型={n['note_type']}，记录人={'教练' if n['author_role']=='coach' else '运动员'}）")
    if history:
        lines.append(f"[历史结论] {history['conclusion']}")
    return "\n".join(lines)


def _skeleton_json(report: dict, notes: list[dict], history) -> str:
    """结构化骨架（供 LLM 读取与 mock 应答），与 sections 数值同源。"""
    payload = {
        "granularity": report["granularity"],
        "window_key": report["window_key"],
        "athlete": report["athlete"],
        "sections": [
            {"title": s["title"], "content": s["content"], "evidence": s["evidence"]}
            for s in report["sections"]
        ],
        "notes": [{"note_type": n["note_type"], "content": n["content"],
                   "author_role": n["author_role"]} for n in notes],
    }
    if history:
        payload["history_conclusion"] = history["conclusion"]
    return json.dumps(payload, ensure_ascii=False)


def _sources(report: dict, notes: list[dict], history) -> list[dict]:
    """溯源列表：每段引用 type/ref/title/摘要，供前端折叠展示。"""
    sources: list[dict] = []
    for sec in report["sections"]:
        sources.append({
            "type": "report", "ref": f"{report['granularity']}:{report['window_key']}",
            "title": sec["title"], "summary": "；".join(str(c) for c in sec["content"])[:200],
        })
    rb = report.get("coach_extra", {}).get("rolling_baseline")
    if rb:
        sources.append({"type": "baseline", "ref": "rolling",
                        "title": "滚动基线", "summary": f"n={rb['n_shots']} 平均环={rb.get('avg_score')}"})
    for n in notes:
        sources.append({"type": "note", "ref": f"note:{n['id']}", "title": f"备注·{n['note_type']}",
                        "summary": n["content"]})
    if history:
        sources.append({"type": "memory", "ref": f"memory:{history['id']}",
                        "title": "历史结论", "summary": history["conclusion"]})
    return sources


def _suggested_note(question: str, notes: list[dict]) -> dict | None:
    """可提炼建议（D4）：MVP 只展示不落库；命中备注关键词且有备注时返回卡片。"""
    if not notes:
        return None
    if not any(kw in question for kw in _NOTE_KEYWORDS):
        return None
    n = notes[0]
    return {
        "note_type": n["note_type"], "content": n["content"],
        "author_role": n["author_role"], "source": f"备注 #{n['id']}", "readonly": True,
    }


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# 行首列表序号（1. / 1、 / 1) / （1） / ①）不是数据。不剔除会被 G2 误判为骨架外数字，
# 导致「1. 技术改进」这类正常编号列表被整体降级（1.5B 实测：报 G2 出现骨架外数字 1.0）。
# 句点后加 (?!\d) 以放行行首小数（如「10.16 环」）。
_LIST_MARKER_RE = re.compile(
    r"(?m)^[ \t]*(?:[-*•·]\s*)?(?:"
    r"\d{1,2}\.(?!\d)"
    r"|\d{1,2}[、)）]"
    r"|[（(]\d{1,2}[）)]"
    r"|[①-⑳]"
    r")[ \t]*"
)

# 日期归一：骨架写作「2026-06-29」，模型常回写成「2026年6月29日」，逐段拆分会得到
# 2026/-6/-29 与 2026/6/29 两套互不匹配的数字，被 G2 误判（1.5B 实测：报骨架外数字 6.0）。
# 统一压成纯数字串（YYYYMMDD / YYYYMM）后再抽取，使同一天的两种写法等价。
_DATE_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})(?:\s*[-/.月]\s*(\d{1,2})\s*日?)?")


def _normalize_dates(text: str) -> str:
    def repl(m: re.Match) -> str:
        year, month, day = m.group(1), int(m.group(2)), m.group(3)
        return f"{year}{month:02d}{int(day):02d}" if day else f"{year}{month:02d}"
    return _DATE_RE.sub(repl, text)


def extract_numbers(text: str) -> list[float]:
    normalized = _normalize_dates(_LIST_MARKER_RE.sub("", text))
    return [float(m) for m in _NUM_RE.findall(normalized)]
