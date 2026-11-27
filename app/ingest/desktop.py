# -*- coding: utf-8 -*-
"""桌面端场次快照接入（Archery WPF → 引擎）。

与 v1.2 设备上行不同：桌面端已经明确知道「一场训练」的边界，不能再用
同人同日 + 间隔聚类（会把相邻两场合并，也会把跨天场次拆开）。本模块按
桌面场次 key 直接落 session_id，重复上传整场替换（幂等），并刷新滚动基线。

口径：
- 身份：本地人员 key（GUID）→ HMAC athlete_id，原值不落库
- 时间：接受带 Z / 偏移的 ISO8601；无时区按 config.timezone（默认 Asia/Shanghai）解释
- 坐标：桌面已是 mm，原样入库；脱靶坐标置 NULL，避免 0,0 进入离散度
- 环值：miss=true → score=0、hit=False；否则用桌面环值（0–10）
- 心率 <=0、风速/风向 <0 视为缺测；mcr_t（撒放用时，秒）<=0 视为缺测
- 弓种：桌面暂未采集，缺省「反曲弓」；后续档案补字段后直接透传
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_config
from app.ingest.align import align_session
from app.ingest.base import SessionRaw, ShotRaw
from app.ingest.identity import pseudonymize_athlete
from app.memory.baseline import refresh_rolling_after_import
from app.store.database import Database

logger = logging.getLogger("engine.ingest.desktop")

SITE = "desktop"
MAX_SHOTS = 2000
BOW_TYPE_MAP = {
    "recurve": "反曲弓",
    "compound": "复合弓",
    "反曲弓": "反曲弓",
    "复合弓": "复合弓",
}


def normalize_bow_type(value: str | None) -> str:
    return BOW_TYPE_MAP.get((value or "").strip(), "反曲弓")


def to_utc_ms_str(value: str) -> str:
    """ISO8601 → UTC 毫秒字符串（YYYY-MM-DDTHH:MM:SS.mmmZ）。无时区按引擎时区解释。"""
    text = (value or "").strip()
    if not text:
        raise ValueError("时间不能为空")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"时间格式非法：{value}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_config().timezone))
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def session_id_for(athlete_id: str, session_key: str) -> str:
    """桌面场次稳定 ID：同一运动员同一 key 重传得到同一 session_id。"""
    safe = "".join(ch for ch in (session_key or "") if ch.isalnum() or ch in "-_")[:64]
    if not safe:
        raise ValueError("sessionKey 不能为空")
    return f"d_{athlete_id}_{safe}"


def _positive(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    if number != number:  # NaN
        return None
    return number if number > 0 else None


def _wind(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    if number != number or number < 0:
        return None
    return number


def ingest_desktop_session(
    db: Database,
    athlete: dict,
    session: dict,
    shots: list[dict],
    on_imported=refresh_rolling_after_import,
) -> dict:
    """写入一场桌面训练快照 → 引擎事实层，返回脱敏 ID 与统计。"""
    athlete_key = (athlete.get("key") or "").strip()
    session_key = (session.get("key") or "").strip()
    if not athlete_key:
        raise ValueError("athlete.key 不能为空")
    if not session_key:
        raise ValueError("session.key 不能为空")
    if not shots:
        raise ValueError("shots 不能为空")
    if len(shots) > MAX_SHOTS:
        raise ValueError(f"单场上限 {MAX_SHOTS} 箭")

    cfg = get_config()
    athlete_id = pseudonymize_athlete(athlete_key)
    session_id = session_id_for(athlete_id, session_key)
    bow_type = normalize_bow_type(session.get("bow_type") or athlete.get("bow_type"))
    distance_m = int(session.get("distance_m") or cfg.store.default_distance_m)

    ordered = sorted(shots, key=lambda s: int(s.get("seq") or 0))
    raw_shots: list[ShotRaw] = []
    warnings: list[str] = []
    has_hr = has_wind = has_mcr = False
    for index, shot in enumerate(ordered, start=1):
        miss = bool(shot.get("is_miss"))
        score = 0.0 if miss else float(shot.get("score") or 0)
        x_mm = None if miss else _number(shot.get("x_mm"))
        y_mm = None if miss else _number(shot.get("y_mm"))
        hr = _positive(shot.get("heart_rate"))
        wind_speed = _wind(shot.get("wind_speed"))
        wind_dir = _wind(shot.get("wind_dir_deg"))
        mcr_t = _positive(shot.get("mcr_t"))
        has_hr = has_hr or hr is not None
        has_wind = has_wind or wind_speed is not None
        has_mcr = has_mcr or mcr_t is not None
        if not miss and (x_mm is None or y_mm is None):
            warnings.append(f"第 {index} 箭缺落点坐标，离散度按缺测处理")
        raw_shots.append(ShotRaw(
            athlete_id=athlete_id,
            session_id=session_id,
            shot_seq=index,
            score=score,
            hit=not miss,
            x_mm=x_mm,
            y_mm=y_mm,
            mcr_t=mcr_t,
            hr=None if hr is None else int(round(hr)),
            wind_speed=wind_speed,
            wind_dir_deg=wind_dir,
            shooting_mode=0 if shot.get("is_trial") else 1,
            bow_type=bow_type,
            shot_time_utc=to_utc_ms_str(shot.get("ts") or ""),
        ))

    if not has_hr:
        warnings.append("本场无心率数据，心率相关结论不输出")
    if not has_wind:
        warnings.append("本场无风数据，风相关结论不输出")
    if not has_mcr:
        warnings.append("本场无撒放用时（mcr_t），撒放一致性结论不输出")

    raw = SessionRaw(
        session_id=session_id,
        athlete_id=athlete_id,
        shots=raw_shots,
        distance_m=distance_m,
        site=SITE,
    )
    dim, rows = align_session(raw)

    with db.write_transaction():
        db.replace_desktop_session(dim, rows)
        _upsert_profile(db, athlete_id, athlete, bow_type)
    if on_imported is not None:
        try:
            on_imported(db, athlete_id, [session_id])
        except Exception as exc:  # 记忆层异常不阻断导入
            logger.warning("桌面场次导入后记忆更新失败（跳过）：%s", exc)

    logger.info("桌面场次已导入 athlete=%s session=%s shots=%d bow=%s",
                athlete_id, session_id, len(raw_shots), bow_type)
    return {
        "athleteId": athlete_id,
        "sessionId": session_id,
        "sessionKey": session_key,
        "bowType": bow_type,
        "distanceM": distance_m,
        "shotCount": len(raw_shots),
        "sessionTimeUtc": dim["session_time_utc"],
        "revision": db.session_revision(session_id),
        "dbInstanceId": db.db_instance_id,
        "warnings": warnings,
    }


def _number(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return None if number != number else number


def _upsert_profile(db: Database, athlete_id: str, athlete: dict, bow_type: str) -> None:
    """合并档案：只覆盖名称/弓种，保留引擎侧已有字段（性别、等级等）。"""
    current = db.get_profile(athlete_id)
    profile = dict(current) if current else {}
    name = (athlete.get("name") or "").strip()
    profile.update({
        "athlete_id": athlete_id,
        "name": name or profile.get("name"),
        "bow_type": bow_type,
    })
    db.upsert_profile(profile)


__all__ = ["ingest_desktop_session", "session_id_for", "to_utc_ms_str", "normalize_bow_type"]
