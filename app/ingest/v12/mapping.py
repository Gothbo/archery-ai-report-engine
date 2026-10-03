# -*- coding: utf-8 -*-
"""v1.2 字段 → 引擎内部事实的映射（纯函数，可单测）。

口径（均出自接口总文档 v1.2 / 对接包 schema）：
- 时间：UTC Z 毫秒，统一格式化为 YYYY-MM-DDTHH:MM:SS.mmmZ（与 shot_fact 现有字符串排序口径一致）
- 锚点：shot_time_utc = releaseTime（§3.1 离弦=全局锚点）；若将来 schema 放宽允许缺失，退回 hitTime（§4.2）
- ID：shotId/scoreId 原样保留为字符串（19 位雪花 ID > 2^53，禁止转 number）
- 单位：dt2 x/y 为 cm → 引擎内部 *_mm（×10）；dt7 已是 mm；flightTimeMs/rmssdMs/R-R 为 ms 原样
- 缺测哨兵（§十）：heartRate<=0 → None；windSpeed/windDirection<0（-1）→ None；
  offsetM 随 windSpeed 缺测（或自身 -1）→ None
- 弓种：bowType 英文枚举 → config.store.v12_bow_type_map（默认 recurve=反曲弓 / compound=复合弓）
- 身份：athleteId → HMAC 脱敏 athlete_id（app/ingest/identity.py），原值不落库
- isGood：v1.2 没有该字段，本适配层不读取、不作作废信号（let down 信号待确认点 #16）
"""
from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

from app.config import get_config
from app.ingest.identity import pseudonymize_athlete
from app.ingest.v12.validate import parse_utc_z

CM_TO_MM = 10.0


def utc_ms_str(value: str | None) -> str | None:
    dt = parse_utc_z(value) if value else None
    if dt is None:
        return None
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def local_date_of(utc_iso: str) -> str:
    dt = parse_utc_z(utc_iso)
    return dt.astimezone(ZoneInfo(get_config().timezone)).strftime("%Y%m%d")


def hr_or_none(v) -> int | None:
    if v is None or v <= 0:
        return None
    return int(round(v))


def wind_or_none(v) -> float | None:
    if v is None or v != v or v < 0:
        return None
    return float(v)


def cm_to_mm(v) -> float | None:
    return None if v is None else round(float(v) * CM_TO_MM, 3)


def map_bow_type(bow: str) -> str:
    return get_config().store.v12_bow_type_map.get(bow, bow)


def map_dt2(data: dict) -> dict:
    """dt2 弹着 → v12_shot 行（不含 message_id/received_at）。"""
    release = utc_ms_str(data.get("releaseTime"))
    hit = utc_ms_str(data.get("hitTime"))
    anchor = release or hit
    wind_speed = wind_or_none(data.get("windSpeed"))
    offset = data.get("offsetM")
    offset_m = None if wind_speed is None or offset is None or offset < 0 else float(offset)
    score = float(data["score"])
    return {
        "shot_id": data["shotId"],
        "score_id": data.get("scoreId"),
        "athlete_id": pseudonymize_athlete(data["athleteId"]),
        "lane": data.get("lane"),
        "shot_seq": data.get("shotSeq"),
        "shot_time_utc": anchor,
        "local_date": local_date_of(anchor),
        "release_time_utc": release,
        "hit_time_utc": hit,
        "flight_time_ms": data.get("flightTimeMs"),
        "score": score,
        "inner_ten": None if data.get("innerTen") is None else int(bool(data["innerTen"])),
        "x_mm": cm_to_mm(data.get("x")),
        "y_mm": cm_to_mm(data.get("y")),
        "shooting_mode": int(data["shootingMode"]),
        "bow_type": map_bow_type(data["bowType"]),
        "target_type": data.get("targetType"),
        "hr": hr_or_none(data.get("heartRate")),
        "wind_speed": wind_speed,
        "wind_dir_deg": wind_or_none(data.get("windDirection")),
        "offset_m": offset_m,
    }


def dt2_warnings(data: dict) -> list[str]:
    """不拦截、只提示的一致性检查（§六自检：flightTimeMs = hitTime − releaseTime）。"""
    out: list[str] = []
    rel, hit = parse_utc_z(data.get("releaseTime")), parse_utc_z(data.get("hitTime"))
    ft = data.get("flightTimeMs")
    if rel and hit:
        gap_ms = (hit - rel).total_seconds() * 1000
        if gap_ms < 0:
            out.append("hitTime 早于 releaseTime（疑似时钟/锚点异常）")
        elif ft is not None and abs(gap_ms - ft) > 2:
            out.append(f"flightTimeMs={ft} 与 hitTime−releaseTime={gap_ms:.0f}ms 不一致")
    return out


def map_sample(data_type: int, data: dict) -> dict:
    """dt1 心率 / dt4 风 → v12_sample 行。"""
    import json

    row = {
        "data_type": data_type,
        "sample_time_utc": utc_ms_str(data["timestamp"]),
        "shot_id": data.get("shotId"),
        "shot_seq": data.get("shotSeq"),
        "lane": data.get("lane"),
        "hr": None, "rr_intervals_ms": None, "rmssd_ms": None,
        "wind_speed": None, "wind_dir_deg": None, "temp_c": None, "humidity_pct": None,
    }
    if data_type == 1:
        hr = hr_or_none(data.get("heartRate"))
        row["hr"] = hr
        rr = data.get("rrIntervalsMs") or []
        # 心率缺测时该窗 R-R/RMSSD 不填充（§十：不出 HRV 结论）
        row["rr_intervals_ms"] = json.dumps(rr) if hr is not None and rr else None
        row["rmssd_ms"] = float(data["rmssdMs"]) if hr is not None and data.get("rmssdMs") is not None else None
    else:
        row["wind_speed"] = wind_or_none(data.get("windSpeed"))
        row["wind_dir_deg"] = wind_or_none(data.get("windDirection"))
        row["temp_c"] = data.get("tempC")
        row["humidity_pct"] = data.get("humidityPct")
    return row
