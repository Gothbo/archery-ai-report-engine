# -*- coding: utf-8 -*-
"""上下文组装器（B1-3）：确定性检索（结构化优先，不引入向量）。

数据源与报告生成器同源，保证「对话里的数字能在报告里找到」（G2 承诺）：
- 档案 athlete_profile
- 目标窗口已保存的报告正文（未指定窗口时取最近一份）
- 历史结论（同粒度最近 1 条 judgement，沿用记忆契约 B2/D5）
- active 备注（14 天窗口，双视角过滤 A2/D9）
- 滚动基线 coach_extra.rolling_baseline

输出：skeleton（结构化结论骨架 + 全部数值）、sources（溯源列表）、
numbers（骨架全部数字，供 G2）、version（上下文版本键）、
suggested_note（可提炼建议，MVP 只展示不落库 D4）。

预留：retrieve_semantic() 为自由文本 RAG 接入点（bge-m3 储备，P2.3d），本轮不实现。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Callable

from app.config import get_config
from app.memory.notes import list_notes_for_view
from app.memory.selective import select_notes_for_context
from app.reports.generator import SessionNotFound, public_view, read_report_snapshot
from app.reports.window import GRANULARITIES, WindowKey
from app.store.database import Database

_NOTE_KEYWORDS = ("伤", "疼", "痛", "目标", "建议", "注意", "调整", "恢复", "改进")

# 预留：自由文本语义检索接入点（P2.3d，bge-m3 储备）。本轮不实现，保持确定性检索优先。
_retrieve_semantic: Callable[..., list[dict]] | None = None


class ReportContextNotFound(ValueError):
    """The athlete or requested saved report is unavailable."""


class ReportSnapshotUnavailable(ReportContextNotFound):
    """The selected report has no available saved body."""


def _saved_report(db: Database, athlete_id: str, key: WindowKey, view: str, report_id: str | None) -> dict:
    if key.granularity != "daily" or key.is_calendar_day:
        key.bounds(get_config())
    if key.granularity == "weekly":
        year, week = key.value.split("-", 1)
        date.fromisocalendar(int(year), int(week.removeprefix("W")), 1)

    if report_id is None:
        if key.session_id:
            session = db.session_of(key.session_id)
            if session is None or session["athlete_id"] != athlete_id:
                raise SessionNotFound(f"运动员 {athlete_id} 没有场次 {key.session_id}")
        rows = db.query(
            """SELECT report_id FROM report_cache WHERE athlete_id=? AND granularity=? AND window_key=?
               AND report_json IS NOT NULL ORDER BY generated_at_utc DESC, rowid DESC LIMIT 1""",
            (athlete_id, key.granularity, key.text),
        )
        if not rows:
            raise ReportSnapshotUnavailable("报告不存在或已更新，请在报告页重新打开")
        report_id = rows[0]["report_id"]
    try:
        return read_report_snapshot(db, athlete_id, key.granularity, key.text, view, report_id)
    except ValueError as exc:
        raise ReportSnapshotUnavailable(str(exc)) from exc


def _report_key(db: Database, athlete_id: str, granularity: str | None,
                window_key: str | None, report_id: str | None) -> WindowKey:
    if granularity is not None:
        if granularity not in GRANULARITIES:
            raise ValueError(f"granularity 必须为 {GRANULARITIES}")
        if window_key is None:
            raise ValueError("指定 granularity 时必须同时指定 window_key")
        return WindowKey.parse(granularity, window_key)
    if report_id:
        latest = db.cached_report_by_id(report_id)
        if not latest or latest["athlete_id"] != athlete_id:
            raise ReportSnapshotUnavailable("报告不存在或已更新，请在报告页重新打开")
    else:
        latest = db.latest_report_window(athlete_id)
        if not latest:
            raise ValueError("运动员暂无已生成报告：请先在报告区生成一份，再发起对话")
    return WindowKey.parse(latest["granularity"], latest["window_key"])


def assemble_context(db: Database, athlete_id: str, question: str,
                     granularity: str | None = None, window_key: str | None = None,
                     view: str = "athlete", report_id: str | None = None, *,
                     strict_expression: bool = False) -> dict:
    """Build display and model context from one visible, already saved report."""
    from app.rag.expression import expression_input

    with db.lock:
        profile = db.get_profile(athlete_id)
        if profile is None:
            raise ReportContextNotFound(f"运动员 {athlete_id} 不存在")
        key = _report_key(db, athlete_id, granularity, window_key, report_id)
        report = _saved_report(db, athlete_id, key, view, report_id)
        notes = select_notes_for_context(list_notes_for_view(db, athlete_id, view), question)
        rule_pack = report.get("rule_pack")
        history = db.latest_memory(
            athlete_id, key.granularity, rule_pack_version=rule_pack["version"] if rule_pack else None)

    # The legacy Q&A and SSE contracts share the two aggregate baseline values across views.
    baseline = {}
    if view == "coach" or not strict_expression:
        baseline = {k: v for k, v in report.get("coach_extra", {}).get("rolling_baseline", {}).items()
                    if k in ("n_shots", "avg_score") and type(v) in (int, float)}

    # 报告正文自带「近期备注 / 辅助信息」备注行（生成时未看问题），会绕过相关性召回；
    # 这里先剥掉，再由选中的 notes 统一注入，保证无关旧事不被带进对话。
    report = _without_notes(public_view(report, view))
    ctx = expression_input(report, notes, history, profile["name"], view, baseline=baseline, question=question)
    ctx["strict_expression"] = strict_expression
    if strict_expression:
        return ctx
    return {
        **ctx,
        "athlete_id": athlete_id,
        "question": question,
        "granularity": key.granularity,
        "window_key": key.text,
        "skeleton": _skeleton_text(report, notes, history, baseline),
        "sources": _sources(report, notes, history, baseline),
        "suggested_note": _suggested_note(question, notes),
    }


def check_context_is_current(db: Database, ctx: dict, view: str) -> None:
    """Reject an answer if its report or a cited note changed while the model was running."""
    with db.lock:
        if not db.cached_report_by_id(ctx["report_id"]):
            raise ReportSnapshotUnavailable("报告不存在或已更新，请在报告页重新打开")
        cited_notes = {source["ref"]: source["summary"] for source in ctx["sources"] if source["type"] == "note"}
        if cited_notes:
            visible_notes = {f"note:{note['id']}": note["content"]
                             for note in list_notes_for_view(db, ctx["athlete_id"], view)}
            if any(visible_notes.get(ref) != text for ref, text in cited_notes.items()):
                raise ReportContextNotFound("引用的备注已变更，请重新打开报告后重试")


def _skeleton_text(report: dict, notes: list[dict], history, baseline: dict) -> str:
    lines: list[str] = []
    for sec in report["sections"]:
        lines.append(f"[{sec['title']}] " + "；".join(str(c) for c in sec["content"]))
    if baseline:
        lines.append(f"[滚动基线] n={baseline['n_shots']} 平均环={baseline.get('avg_score')}")
    for n in notes:
        lines.append(f"[备注] {n['content']}（类型={n['note_type']}，记录人={'教练' if n['author_role']=='coach' else '运动员'}）")
    if history:
        lines.append(f"[历史结论] {history['conclusion']}")
    return "\n".join(lines)


def _sources(report: dict, notes: list[dict], history, baseline: dict) -> list[dict]:
    """溯源列表：每段引用 type/ref/title/摘要，供前端折叠展示。"""
    sources: list[dict] = []
    for sec in report["sections"]:
        sources.append({
            "type": "report", "ref": f"{report['granularity']}:{report['window_key']}",
            "title": sec["title"], "summary": "；".join(str(c) for c in sec["content"])[:200],
        })
    if baseline:
        sources.append({"type": "baseline", "ref": "rolling",
                        "title": "滚动基线", "summary": f"n={baseline['n_shots']} 平均环={baseline.get('avg_score')}"})
    for n in notes:
        sources.append({"type": "note", "ref": f"note:{n['id']}", "title": f"备注·{n['note_type']}",
                        "summary": n["content"],
                        "provenance": n.get("provenance") or "manual",
                        "created_at_utc": n.get("created_at_utc")})
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

# 报告生成/读取时会把备注渲染成「[goal] 内容（记录人：运动员）」，需在相关性召回前剥掉。
_NOTE_LINE_RE = re.compile(r"^\[(?:injury|goal|coach_note|other)\] .*（记录人：")


def _without_notes(report: dict) -> dict:
    sections = []
    for sec in report.get("sections", []):
        if sec.get("key") == "notes":
            continue
        content = [c for c in sec.get("content", []) if not _NOTE_LINE_RE.match(str(c))]
        if sec.get("key") == "auxiliary" and not content:
            content = ["本窗口暂无辅助信息"]
        sections.append({**sec, "content": content})
    return {**report, "sections": sections}

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
