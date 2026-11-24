# -*- coding: utf-8 -*-
"""API · 数据导入（D3：POST /ingest/mock；M5 增 /ingest/sqlite；v1.2 上行接口 /ingest/v12）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.ingest import ingest_source
from app.ingest.desktop import ingest_desktop_session
from app.ingest.mock_source import MockSource
from app.ingest.sqlite_source import SQLiteSource
from app.ingest.v12 import ingest_messages
from app.ingest.v12.receiver import IN_PHASE, RAW_ONLY
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


class _CamelModel(BaseModel):
    """桌面端 JSON 用 camelCase（C# System.Text.Json 默认策略），同时兼容 snake_case。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class DesktopAthlete(_CamelModel):
    key: str = Field(min_length=1, description="桌面本地人员 ID（GUID），不是身份证号")
    name: str | None = None
    bow_type: str | None = Field(default=None, description="recurve / compound / 反曲弓 / 复合弓")


class DesktopSession(_CamelModel):
    key: str = Field(min_length=1, description="桌面场次 ID（TrainingSession.Id）")
    started_at: str
    ended_at: str | None = None
    distance_m: int | None = None
    subject: str | None = None
    project: str | None = None


class DesktopShot(_CamelModel):
    seq: int
    x_mm: float | None = None
    y_mm: float | None = None
    score: float = 0
    is_x: bool = False
    is_miss: bool = False
    is_trial: bool = False
    ts: str
    heart_rate: float | None = None
    wind_speed: float | None = None
    wind_dir_deg: float | None = None
    mcr_t: float | None = Field(default=None, description="撒放用时（秒）")


class DesktopSnapshot(_CamelModel):
    athlete: DesktopAthlete
    session: DesktopSession
    shots: list[DesktopShot]


@router.post("/desktop")
def ingest_desktop(body: DesktopSnapshot) -> dict:
    """桌面端整场快照（Archery WPF）。

    - 按 session.key 直接落场次，保留桌面场次边界；重复上传整场替换（幂等）
    - 返回 HMAC 后的 athleteId 与引擎 sessionId，可直接调用
      `POST /api/v1/athletes/{athleteId}/reports/daily?session_id={sessionId}`
    """
    try:
        result = ingest_desktop_session(
            get_database(),
            body.athlete.model_dump(),
            body.session.model_dump(),
            [shot.model_dump() for shot in body.shots],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", **result}


V12_MAX_BATCH = 500


@router.post("/v12")
def ingest_v12(payload: Any = Body(..., description="单条 v1.2 消息（JSON 对象）或消息数组（批量）")) -> dict:
    """接收《射箭电子靶数据接口 v1.2》上行消息（信封 schemaVersion/messageId/dataType/data）。

    - 协议版本：schemaVersion "1.1"（原对接包 schema）与 "1.2"（修订版 schema：脱靶 miss、
      releaseTime 可选）均接收，按版本分别校验；其他版本判为 invalid
    - 单条：请求体为一个消息对象；批量：请求体为消息数组（≤500 条）
    - 逐条返回结果：accepted / duplicate（messageId 或 shotId 已受理）/ invalid（附原因）/
      unsupported（本期外 dataType，或 dt2 缺 athleteId 无法归属）
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


@router.get("/v12/last-received")
def v12_last_received() -> dict:
    """各类 v1.2 消息「引擎最后受理时间」（UTC，PR #4，门户设备状态 / 新鲜度用）。

    - 只统计受理成功的消息（重复 / 非法 / 不支持的不更新）；时间是引擎收到的时间，不是设备时间
    - 从未受理 → null；宿主 SQLite 导入路径没有「收到时间」，不计入（source 固定 "v12"）
    - 只返回时间，不返回设备号、靶位、运动员
    """
    last = get_database().ingest_last_received(sorted(IN_PHASE | RAW_ONLY))
    now = datetime.now(timezone.utc)
    body: dict = {f"dt{dt}": t for dt, t in last.items()}
    body["source"] = "v12"
    body["as_of_utc"] = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    return body
