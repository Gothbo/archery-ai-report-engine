# -*- coding: utf-8 -*-
"""记忆层 · 人工备注 CRUD + 角色可见性过滤（拍板 A2/D9）。

- 录入：note_type + content + author_role（前端传）+ actor_id（审计）
- 归属校验（A6）：写/删强制带 athlete_id（SQL WHERE athlete_id=?）
- 软删（B1）：DELETE → status='closed'，保留审计
- 显示：默认近 N 天 active；运动员版按 note_type 过滤（仅 injury/goal，不论录入人），
  教练版全量；author_role 仅作展示标注
"""
from __future__ import annotations

import logging

from app.config import get_config
from app.store.database import Database
from app.timeutil import iso_now_utc

logger = logging.getLogger("engine.memory.notes")

VALID_NOTE_TYPES = ("injury", "goal", "coach_note", "other")
VALID_ROLES = ("athlete", "coach")
VALID_PROVENANCE = ("conversation", "report", "manual")


def add_note(db: Database, athlete_id: str, note_type: str, content: str,
             author_role: str, actor_id: str, *,
             provenance: str = "manual", confirmed: bool = True,
             quote_text: str | None = None) -> int:
    """写入一条备注/记忆。

    provenance=conversation 且 confirmed=False → 候选记忆（confirmed_at_utc 为空），
    不参与召回；确认后才成为正式记忆。手工/自述默认直接生效。
    """
    if note_type not in VALID_NOTE_TYPES:
        raise ValueError(f"note_type 必须为 {VALID_NOTE_TYPES}")
    if author_role not in VALID_ROLES:
        raise ValueError(f"author_role 必须为 {VALID_ROLES}")
    if provenance not in VALID_PROVENANCE:
        raise ValueError(f"provenance 必须为 {VALID_PROVENANCE}")
    if not content.strip():
        raise ValueError("content 不能为空")
    created_at = iso_now_utc()
    note = {
        "athlete_id": athlete_id,
        "note_type": note_type,
        "content": content,
        "author_role": author_role,
        "actor_id": actor_id,
        "created_at_utc": created_at,
        "provenance": provenance,
        "quote_text": quote_text,
        "confirmed_at_utc": created_at if confirmed else None,
    }
    nid = db.insert_note(note)
    logger.info("备注已录入 id=%d athlete=%s type=%s role=%s provenance=%s confirmed=%s",
                nid, athlete_id, note_type, author_role, provenance, confirmed)
    return nid


def close_note(db: Database, athlete_id: str, note_id: int) -> bool:
    ok = db.close_note(note_id, athlete_id)
    if ok:
        logger.info("备注已软删 id=%d athlete=%s", note_id, athlete_id)
    return ok


def confirm_note(db: Database, athlete_id: str, note_id: int) -> bool:
    """候选记忆 → 正式记忆（只对未确认的 active 备注生效）。"""
    ok = db.confirm_note(note_id, athlete_id)
    if ok:
        logger.info("候选记忆已确认 id=%d athlete=%s", note_id, athlete_id)
    return ok


def update_note(db: Database, athlete_id: str, note_id: int, content: str) -> dict | None:
    content = content.strip()
    if not content:
        raise ValueError("content 不能为空")
    if not db.update_note_content(note_id, athlete_id, content):
        return None
    row = db.get_note(note_id, athlete_id)
    return dict(row) if row else None


def list_notes_for_view(db: Database, athlete_id: str, view: str, *,
                        days: int | None = None, confirmed: bool = True) -> list[dict]:
    """按视角过滤（A2/D9）：athlete 版含 injury/goal 及本人录入的备注；coach 版全量。

    默认只返回已确认记忆（候选不参与展示/召回）。`days=None` 表示不按展示窗口截断，
    供记忆管理页读取全部档案级记忆；报告「近期备注」由调用方显式传入配置天数。
    """
    cfg = get_config()
    rows = db.list_notes(athlete_id, status="active", days=days, confirmed=confirmed)
    notes = [dict(r) for r in rows]
    if view == "athlete":
        visible = set(cfg.notes_visibility.athlete_visible_types)
        notes = [n for n in notes
                 if n["note_type"] in visible or n["author_role"] == "athlete"]
    # author_role 仅作展示标注，不需要剥掉
    return notes


def list_candidates(db: Database, athlete_id: str, view: str = "athlete") -> list[dict]:
    """待确认候选：active 且未确认。候选不参与召回。"""
    rows = db.list_notes(athlete_id, status="active", days=None, confirmed=False)
    notes = [dict(r) for r in rows]
    if view == "athlete":
        notes = [n for n in notes if n["author_role"] == "athlete"]
    return notes
