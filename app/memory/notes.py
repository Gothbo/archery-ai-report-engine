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


def add_note(db: Database, athlete_id: str, note_type: str, content: str,
             author_role: str, actor_id: str) -> int:
    if note_type not in VALID_NOTE_TYPES:
        raise ValueError(f"note_type 必须为 {VALID_NOTE_TYPES}")
    if author_role not in VALID_ROLES:
        raise ValueError(f"author_role 必须为 {VALID_ROLES}")
    if not content.strip():
        raise ValueError("content 不能为空")
    note = {
        "athlete_id": athlete_id,
        "note_type": note_type,
        "content": content,
        "author_role": author_role,
        "actor_id": actor_id,
        "created_at_utc": iso_now_utc(),
    }
    nid = db.insert_note(note)
    logger.info("备注已录入 id=%d athlete=%s type=%s role=%s", nid, athlete_id, note_type, author_role)
    return nid


def close_note(db: Database, athlete_id: str, note_id: int) -> bool:
    ok = db.close_note(note_id, athlete_id)
    if ok:
        logger.info("备注已软删 id=%d athlete=%s", note_id, athlete_id)
    return ok


def list_notes_for_view(db: Database, athlete_id: str, view: str) -> list[dict]:
    """按视角过滤（A2/D9）：athlete 版仅 injury/goal；coach 版全量。"""
    cfg = get_config()
    days = cfg.memory.notes_display_days
    rows = db.list_notes(athlete_id, status="active", days=days)
    notes = [dict(r) for r in rows]
    if view == "athlete":
        visible = set(cfg.notes_visibility.athlete_visible_types)
        notes = [n for n in notes if n["note_type"] in visible]
    # author_role 仅作展示标注，不需要剥掉
    return notes
