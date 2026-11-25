# -*- coding: utf-8 -*-
"""API · 训练详情（逐箭）与备注/记忆（记忆方案 §3.4 + 08 选择性记忆）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import get_config
from app.ingest.desktop import session_id_for
from app.memory.notes import (
    add_note,
    close_note,
    confirm_note,
    list_candidates,
    list_notes_for_view,
    update_note,
)
from app.memory.selective import extract_memories
from app.store.database import get_database

sessions_router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
session_lifecycle_router = APIRouter(prefix="/api/v1/athletes/{athlete_id}/sessions", tags=["sessions"])
notes_router = APIRouter(prefix="/api/v1/athletes/{athlete_id}", tags=["notes"])


@sessions_router.get("/{session_id}")
def session_detail(session_id: str) -> dict:
    db = get_database()
    dim = db.session_of(session_id)
    if not dim:
        raise HTTPException(status_code=404, detail="场次不存在（尚未导入）")
    lifecycle = db.session_lifecycle(dim["athlete_id"], session_id)
    excluded = bool(lifecycle and lifecycle["excluded_at_utc"])
    rows = db.shots_of_session(session_id)
    return {
        "session_id": session_id,
        "dim": dict(dim) if dim else None,
        "shots": [dict(r) for r in rows],
        "excluded": excluded,
        "excluded_at_utc": lifecycle["excluded_at_utc"] if excluded else None,
    }


class SessionLifecycleRequest(BaseModel):
    actor_id: str | None = None
    reason: str | None = None


def _session_lifecycle(athlete_id: str, session_id: str, body: SessionLifecycleRequest, action: str) -> dict:
    db = get_database()
    dim = db.session_of(session_id)
    if dim is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": "场次不存在"})
    if dim["athlete_id"] != athlete_id:
        raise HTTPException(status_code=404, detail={"code": "session_not_owned", "message": "场次不属于该运动员"})
    actor = body.actor_id or "unknown"
    reason = body.reason or "unspecified"
    if action == "exclude":
        already, timestamp = db.set_session_excluded(athlete_id, session_id, actor, reason)
        return {"status": "excluded", "athlete_id": athlete_id, "session_id": session_id,
                "excluded_at_utc": timestamp, "already_excluded": already}
    already, timestamp = db.set_session_active(athlete_id, session_id, actor, reason)
    return {"status": "active", "athlete_id": athlete_id, "session_id": session_id,
            "restored_at_utc": timestamp, "already_active": already}


def _desktop_session_id(athlete_id: str, session_key: str) -> str:
    try:
        return session_id_for(athlete_id, session_key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "session_not_found", "message": str(exc)}) from exc


@session_lifecycle_router.post("/by-key/{session_key}/exclude")
def exclude_session_by_key(athlete_id: str, session_key: str, body: SessionLifecycleRequest = SessionLifecycleRequest()) -> dict:
    return _session_lifecycle(athlete_id, _desktop_session_id(athlete_id, session_key), body, "exclude")


@session_lifecycle_router.post("/by-key/{session_key}/restore")
def restore_session_by_key(athlete_id: str, session_key: str, body: SessionLifecycleRequest = SessionLifecycleRequest()) -> dict:
    return _session_lifecycle(athlete_id, _desktop_session_id(athlete_id, session_key), body, "restore")


@session_lifecycle_router.post("/{session_id}/exclude")
def exclude_session(athlete_id: str, session_id: str, body: SessionLifecycleRequest) -> dict:
    return _session_lifecycle(athlete_id, session_id, body, "exclude")


@session_lifecycle_router.post("/{session_id}/restore")
def restore_session(athlete_id: str, session_id: str, body: SessionLifecycleRequest) -> dict:
    return _session_lifecycle(athlete_id, session_id, body, "restore")


class NoteCreate(BaseModel):
    note_type: str
    content: str
    author_role: str
    actor_id: str  # 录入人雪花 ID（审计；归属校验 WHERE athlete_id，A6）


@notes_router.get("/notes")
def list_notes(athlete_id: str, view: str = Query(default="athlete", pattern="^(athlete|coach)$")) -> dict:
    db = get_database()
    days = get_config().memory.notes_display_days
    notes = list_notes_for_view(db, athlete_id, view, days=days)
    return {"athlete_id": athlete_id, "view": view, "notes": notes}


@notes_router.get("/memory-notes")
def list_memory_notes(
    athlete_id: str, view: str = Query(default="athlete", pattern="^(athlete|coach)$")) -> dict:
    """档案级记忆查询（不带展示窗口）：供运动员查看/编辑/删除。候选不在其中。"""
    db = get_database()
    return {"athlete_id": athlete_id, "view": view,
            "notes": list_notes_for_view(db, athlete_id, view)}


@notes_router.get("/memory-candidates")
def list_memory_candidates(athlete_id: str) -> dict:
    db = get_database()
    return {"athlete_id": athlete_id, "candidates": list_candidates(db, athlete_id)}


class MemoryExtract(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    actor_id: str


@notes_router.post("/memories/extract")
def extract_memory(athlete_id: str, body: MemoryExtract) -> dict:
    """从一次发言中提炼记忆：普通信息直接保存，伤病等敏感信息停在候选。

    只返回提炼后的短句，不保存聊天全文。
    """
    db = get_database()
    return extract_memories(db, athlete_id, body.text, body.actor_id)


@notes_router.post("/memory-candidates/{note_id}/confirm")
def confirm_memory(athlete_id: str, note_id: int) -> dict:
    db = get_database()
    if not confirm_note(db, athlete_id, note_id):
        raise HTTPException(status_code=404, detail="候选记忆不存在、已确认或不属于该运动员")
    row = db.get_note(note_id, athlete_id)
    return {"status": "ok", "note": dict(row) if row else None}


@notes_router.delete("/memory-candidates/{note_id}")
def reject_memory(athlete_id: str, note_id: int) -> dict:
    db = get_database()
    if not close_note(db, athlete_id, note_id):
        raise HTTPException(status_code=404, detail="候选记忆不存在、已关闭或不属于该运动员")
    return {"status": "ok", "note_id": note_id}


class NoteUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=500)


@notes_router.patch("/memory-notes/{note_id}")
def update_memory(athlete_id: str, note_id: int, body: NoteUpdate) -> dict:
    db = get_database()
    try:
        note = update_note(db, athlete_id, note_id, body.content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if note is None:
        raise HTTPException(status_code=404, detail="记忆不存在、已关闭或不属于该运动员")
    return note


@notes_router.delete("/memory-notes/{note_id}")
def delete_memory(athlete_id: str, note_id: int) -> dict:
    db = get_database()
    if not close_note(db, athlete_id, note_id):
        raise HTTPException(status_code=404, detail="记忆不存在、已关闭或不属于该运动员")
    return {"status": "ok", "note_id": note_id}


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
        row = db.get_note(note_id, athlete_id)
        if row is None or row["status"] != "closed":
            raise HTTPException(status_code=404, detail="备注不存在或不属于该运动员")
    return {"status": "ok", "note_id": note_id}


@notes_router.get("/memories")
def list_memories(athlete_id: str) -> dict:
    db = get_database()
    rows = db.list_memories(athlete_id)
    return {"athlete_id": athlete_id, "memories": [dict(r) for r in rows]}
