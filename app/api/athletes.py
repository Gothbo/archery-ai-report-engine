# -*- coding: utf-8 -*-
"""API · 运动员档案 / 基线 / 训练列表（D3 + 记忆方案 §3.4）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.memory.baseline import create_anchor_snapshot
from app.store.database import get_database

router = APIRouter(prefix="/api/v1/athletes", tags=["athletes"])


class ProfileUpdate(BaseModel):
    name: str | None = None
    gender: str | None = None
    age: int | None = Field(default=None, ge=1, le=120)
    bow_type: str | None = None
    hand: str | None = None
    level: str | None = None


@router.get("")
def list_athletes() -> dict:
    db = get_database()
    # last_session_utc：供前端默认选中「最近有训练」的运动员（列表按姓名排序，
    # 直接取第一个可能落到久未训练的档案 → 打开即「样本不足」）
    rows = db.athletes_with_last_session()
    return {"athletes": [dict(r) for r in rows]}


@router.get("/{athlete_id}")
def get_athlete(athlete_id: str) -> dict:
    db = get_database()
    profile = db.get_profile(athlete_id)
    anchor = db.latest_snapshot(athlete_id, "anchor")
    rolling = db.latest_snapshot(athlete_id, "rolling")
    return {
        "athlete_id": athlete_id,
        "profile": dict(profile) if profile else None,
        "baseline": {
            "anchor": dict(anchor) if anchor else None,
            "rolling": dict(rolling) if rolling else None,
        },
    }


@router.get("/{athlete_id}/profile")
def get_profile(athlete_id: str) -> dict:
    db = get_database()
    profile = db.get_profile(athlete_id)
    if not profile:
        raise HTTPException(status_code=404, detail="档案不存在（先导入数据或建档案）")
    return dict(profile)


@router.put("/{athlete_id}/profile")
def update_profile(athlete_id: str, body: ProfileUpdate) -> dict:
    """更新档案（upsert）。归属校验：athlete_id 路径参数即归属键（A6 P1 最小实现）。"""
    db = get_database()
    current = db.get_profile(athlete_id)
    merged = dict(current) if current else {}
    for k, v in body.model_dump(exclude_none=True).items():
        merged[k] = v
    merged["athlete_id"] = athlete_id
    db.upsert_profile(merged)
    return {"status": "ok", "athlete_id": athlete_id}


@router.get("/{athlete_id}/sessions")
def list_sessions(athlete_id: str) -> dict:
    db = get_database()
    rows = db.sessions_of_athlete(athlete_id)
    return {"athlete_id": athlete_id, "sessions": [dict(r) for r in rows]}


@router.get("/{athlete_id}/baseline")
def list_baseline(athlete_id: str) -> dict:
    db = get_database()
    rows = db.list_snapshots(athlete_id)
    return {"athlete_id": athlete_id, "snapshots": [dict(r) for r in rows]}


class AnchorCreate(BaseModel):
    bow_type: str
    source_session_id: str


@router.post("/{athlete_id}/baseline/anchor")
def create_anchor(athlete_id: str, body: AnchorCreate) -> dict:
    """建立/重建锚点基线（赛季初/入队测试/换弓种）。可比性元数据随快照落库（A3）。"""
    db = get_database()
    try:
        snap = create_anchor_snapshot(db, athlete_id, body.bow_type, body.source_session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    snap_id = db.insert_baseline_snapshot(snap)
    return {"status": "ok", "snapshot_id": snap_id, "snapshot": snap}
