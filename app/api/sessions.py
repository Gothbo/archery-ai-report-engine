# -*- coding: utf-8 -*-
"""API · 训练详情（逐箭）与备注/记忆（记忆方案 §3.4）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.memory.notes import add_note, close_note, list_notes_for_view
from app.store.database import get_database

sessions_router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
notes_router = APIRouter(prefix="/api/v1/athletes/{athlete_id}", tags=["notes"])


@sessions_router.get("/{session_id}")
def session_detail(session_id: str) -> dict:
    db = get_database()
    rows = db.shots_of_session(session_id)
    if not rows:
        raise HTTPException(status_code=404, detail="场次不存在（尚未导入）")
    dim = db.session_of(session_id)
    return {
        "session_id": session_id,
        "dim": dict(dim) if dim else None,
        "shots": [dict(r) for r in rows],
    }


class NoteCreate(BaseModel):
    note_type: str
    content: str
    author_role: str
    actor_id: str  # 录入人雪花 ID（审计；归属校验 WHERE athlete_id，A6）


@notes_router.get("/notes")
def list_notes(athlete_id: str, view: str = Query(default="athlete", pattern="^(athlete|coach)$")) -> dict:
    db = get_database()
    notes = list_notes_for_view(db, athlete_id, view)
    return {"athlete_id": athlete_id, "view": view, "notes": notes}


@notes_router.post("/notes")
def create_note(athlete_id: str, body: NoteCreate) -> dict:
    db = get_database()
    try:
        nid = add_note(db, athlete_id, body.note_type, body.content, body.author_role, body.actor_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "note_id": nid}


@notes_router.delete("/notes/{note_id}")
def delete_note(athlete_id: str, note_id: int) -> dict:
    """软删（B1）：status→closed，保留审计；归属校验 WHERE athlete_id。"""
    db = get_database()
    ok = close_note(db, athlete_id, note_id)
    if not ok:
        raise HTTPException(status_code=404, detail="备注不存在、已关闭或不属于该运动员")
    return {"status": "ok", "note_id": note_id}


@notes_router.get("/memories")
def list_memories(athlete_id: str) -> dict:
    db = get_database()
    rows = db.list_memories(athlete_id)
    return {"athlete_id": athlete_id, "memories": [dict(r) for r in rows]}
