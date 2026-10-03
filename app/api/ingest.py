# -*- coding: utf-8 -*-
"""API · 数据导入（D3：POST /ingest/mock；M5 增 /ingest/sqlite；v1.2 上行接口 /ingest/v12）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.ingest import ingest_source
from app.ingest.mock_source import MockSource
from app.ingest.sqlite_source import SQLiteSource
from app.ingest.v12 import ingest_messages
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
    """导入 display_sys 真 SQLite 库（M5）。只读消费：身份证→HMAC 脱敏 athlete_id（不可逆），时间转 UTC。"""
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


V12_MAX_BATCH = 500


@router.post("/v12")
def ingest_v12(payload: Any = Body(..., description="单条 v1.2 消息（JSON 对象）或消息数组（批量）")) -> dict:
    """接收《射箭电子靶数据接口 v1.2》上行消息（信封 schemaVersion/messageId/dataType/data）。

    - 单条：请求体为一个消息对象；批量：请求体为消息数组（≤500 条）
    - 逐条返回结果：accepted / duplicate（messageId 或 shotId 已受理）/ invalid（附原因）/
      unsupported（本期外 dataType）
    - 批内单条失败不影响其它条；HTTP 200 表示批次已处理，具体看每条 status
    """
    if isinstance(payload, dict):
        messages = [payload]
    elif isinstance(payload, list):
        messages = payload
    else:
        raise HTTPException(status_code=400, detail="请求体必须是 v1.2 消息对象或消息数组")
    if not messages:
        raise HTTPException(status_code=400, detail="消息数组为空")
    if len(messages) > V12_MAX_BATCH:
        raise HTTPException(status_code=413, detail=f"单批最多 {V12_MAX_BATCH} 条消息")
    out = ingest_messages(get_database(), messages, on_imported=refresh_rolling_after_import)
    return {"status": "ok", **out}
