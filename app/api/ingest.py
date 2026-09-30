# -*- coding: utf-8 -*-
"""API · 数据导入（D3：POST /ingest/mock；M5 增 /ingest/sqlite）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.ingest import ingest_source
from app.ingest.mock_source import MockSource
from app.ingest.sqlite_source import SQLiteSource
from app.memory.baseline import refresh_rolling_after_import
from app.store.database import get_database

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])


@router.post("/mock")
def ingest_mock(mock_path: str | None = None) -> dict:
    """导入 mock 数据集（开发/演示用）。重复导入幂等（session_dim upsert + UNIQUE 跳过）。"""
    try:
        source = MockSource(mock_path) if mock_path else MockSource()
        db = get_database()
        stats = ingest_source(db, source, on_imported=refresh_rolling_after_import)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"mock 数据文件不存在：{exc}") from exc
    except Exception as exc:  # 导入失败返回 400（路径/格式问题）
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "stats": stats}


@router.post("/sqlite")
def ingest_sqlite(db_path: str | None = None) -> dict:
    """导入 display_sys 真 SQLite 库（M5）。只读消费：身份证→雪花 ID 确定性映射，时间转 UTC。"""
    try:
        source = SQLiteSource(db_path)
        db = get_database()
        # 建档已并入 ingest_source：真库无档案表，从 WindSpeedDirection 反查姓名写 athlete_profile（幂等 upsert）
        profiles = source.athlete_profiles()
        stats = ingest_source(db, source, on_imported=refresh_rolling_after_import)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"SQLite 文件不存在：{exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "stats": stats, "athlete_profiles": len(profiles)}
