# -*- coding: utf-8 -*-
"""M5 真数据源：display_sys 的 SQLite 库 → 统一 SessionRaw 契约。

字段语义（源码摸底 + 本模块探查 2026-09）：
- ScoreInfoNew.Score  环值 TEXT（0.0=脱靶；10.9=内十）；IsGood=1 是"好箭"(≥10.2)，不是命中
- 命中 = Score>0（脱靶 359/6823 ≈ 5%，与命中率语义一致）
- ShotType 1/3/7 语义待开发确认 → config.store.shot_type_map 映射（默认 1=试射、3/7=记分）
- ProjectId → 弓种映射 config.store.project_bow_map；未映射走 proj{id}（不同项目基线不混用）
- ShootingTime/CreateDate/CreateTime 均为**本地时间（Asia/Shanghai）无时区** → 转 UTC 存储（A1）
- 运动员主键：真库只有身份证号（IdentityID，18 位含 X 校验位）→ 确定性映射雪花格式 athlete_id
  （纯数字 18 位用 10^18+identity 双射；含 X 用 SHA1 派生），真接入时由登录系统下发正式雪花 ID
- mcr_t 不在本库（撒放用时在弹道消息）→ 一律 NULL（缺测不出结论）
- HR/Wind 按运动员 + 时间窗口最近匹配（HR 30s / Wind 60s，D2）
- 场次聚类：同运动员同日，箭间隔 >session_gap_minutes 拆为两场（M5 口径）
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

from app.config import get_config
from app.ingest.base import SessionRaw, ShotRaw, Source

logger = logging.getLogger("engine.ingest.sqlite")

_SHOT_TABLE = "ScoreInfoNew"
_HR_TABLE = "HeartRateData"
_WIND_TABLE = "WindSpeedDirection"

HR_WINDOW_MS = 30_000
WIND_WINDOW_MS = 60_000


def _local_to_utc_ms(local_dt: datetime) -> int:
    tz = ZoneInfo(get_config().timezone)
    return int(local_dt.replace(tzinfo=tz).astimezone(timezone.utc).timestamp() * 1000)


def _parse_local(ts: str) -> datetime | None:
    """解析 display_sys 的本地时间字符串（2/3/7 位小数不定）。"""
    if not ts:
        return None
    norm = ts.replace("T", " ").strip()
    try:
        return datetime.fromisoformat(norm)
    except ValueError:
        try:  # 7 位小数 fromisoformat 不支持 → 截断到 6 位
            return datetime.fromisoformat(norm[: -7] + norm[-6:])
        except ValueError:
            return None


def _to_utc_iso_ms(local_dt: datetime) -> str:
    tz = ZoneInfo(get_config().timezone)
    utc = local_dt.replace(tzinfo=tz).astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def athlete_id_of(identity) -> str:
    """身份证号 → 雪花格式运动员 ID（确定性；双射优先，X 校验位走 SHA1 派生）。"""
    ident = str(identity).strip().upper()
    if re.fullmatch(r"\d{18}", ident):
        return str(10**18 + int(ident))
    h = hashlib.sha1(ident.encode("utf-8")).hexdigest()
    return str(10**18 + int(h[:14], 16) % 10**18)


def _to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _nearest(shot_ms: int, samples: list[tuple[int, int]], window_ms: int) -> int | None:
    best = None
    for ts, val in samples:
        gap = abs(ts - shot_ms)
        if gap <= window_ms and (best is None or gap < abs(best[0] - shot_ms)):
            best = (ts, val)
    return best[1] if best else None


def _nearest_wind(shot_ms: int, samples: list[tuple[int, float, float]], window_ms: int) -> tuple[float, float] | None:
    best = None
    for ts, speed, deg in samples:
        gap = abs(ts - shot_ms)
        if gap <= window_ms and (best is None or gap < abs(best[0] - shot_ms)):
            best = (ts, speed, deg)
    return (best[1], best[2]) if best else None


class SQLiteSource(Source):
    """从 display_sys SQLite 库导入训练数据（只读，纯消费者）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or (get_config().store.sqlite_source_path or "")

    def athlete_profiles(self) -> dict[str, dict]:
        """{athlete_id: {name, identity_id}}：名字取自 WindSpeedDirection.AthleteName。"""
        out: dict[str, dict] = {}
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT DISTINCT RegisterNum, AthleteName FROM {_WIND_TABLE} WHERE AthleteName IS NOT NULL"
            ).fetchall()
        for reg, name in rows:
            aid = athlete_id_of(reg)
            out.setdefault(aid, {"name": name, "identity_id": str(reg)})
        return out

    def count_score_rows(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {_SHOT_TABLE}").fetchone()[0]

    def iter_sessions(self) -> Iterator[SessionRaw]:
        store = get_config().store
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            score_rows = conn.execute(
                f"""SELECT * FROM {_SHOT_TABLE}
                    WHERE ShootingTime >= ? AND ShootingTime IS NOT NULL
                      AND LENGTH(CAST(IdentityID AS TEXT)) >= 17
                    ORDER BY ShootingTime""",
                (store.sqlite_min_time,),
            ).fetchall()
            hr_map = self._load_hr(conn)
            wind_map = self._load_wind(conn)

        # 场次聚类：同运动员同日、相邻箭间隔 <= gap 归为一场（按时间序保证顺序）
        clusters: list[list[sqlite3.Row]] = []
        for r in score_rows:
            local_dt = _parse_local(r["ShootingTime"])
            if local_dt is None:
                logger.warning("跳过无法解析时间的箭 id=%s", r["Id"])
                continue
            aid = athlete_id_of(r["IdentityID"])
            if clusters:
                prev = clusters[-1][-1]
                prev_dt = _parse_local(prev["ShootingTime"])
                prev_aid = athlete_id_of(prev["IdentityID"])
                if (prev_dt is not None and prev_aid == aid and prev_dt.date() == local_dt.date()
                        and local_dt - prev_dt <= timedelta(minutes=store.session_gap_minutes)):
                    clusters[-1].append(r)
                    continue
            clusters.append([r])

        date_seq: dict[tuple[str, str], int] = {}
        for cluster in clusters:
            first = cluster[0]
            aid = athlete_id_of(first["IdentityID"])
            local_dt = _parse_local(first["ShootingTime"])
            date_s = local_dt.strftime("%Y%m%d") if local_dt else "00000000"
            date_seq[(aid, date_s)] = date_seq.get((aid, date_s), 0) + 1
            session_id = f"{aid}_{date_s}_{date_seq[(aid, date_s)]:02d}"

            shots: list[ShotRaw] = []
            for i, r in enumerate(cluster, start=1):
                dt = _parse_local(r["ShootingTime"])
                shot_ms = _local_to_utc_ms(dt) if dt else None
                shots.append(ShotRaw(
                    athlete_id=aid,
                    session_id=session_id,
                    shot_seq=i,
                    score=_to_float(r["Score"]) or 0.0,
                    hit=bool((_to_float(r["Score"]) or 0.0) > 0.0),
                    x_mm=_to_float(r["X_"]),
                    y_mm=_to_float(r["Y_"]),
                    mcr_t=None,
                    hr=_nearest(shot_ms, hr_map.get(aid, []), HR_WINDOW_MS) if shot_ms else None,
                    wind_speed=None,
                    wind_dir_deg=None,
                    shooting_mode=store.shot_type_map.get(str(r["ShotType"]), 1),
                    bow_type=store.project_bow_map.get(str(r["ProjectId"]), f"proj{r['ProjectId']}"),
                    video_ref=None,
                    shot_time_utc=_to_utc_iso_ms(dt) if dt else "",
                ))
                if shot_ms:
                    nearest = _nearest_wind(shot_ms, wind_map.get(aid, []), WIND_WINDOW_MS)
                    if nearest:
                        shots[-1].wind_speed, shots[-1].wind_dir_deg = nearest

            yield SessionRaw(
                session_id=session_id,
                athlete_id=aid,
                shots=shots,
                distance_m=store.default_distance_m,
                site="suooter",
            )

    # ---- 采样表加载（只读，内存映射）----

    def _load_hr(self, conn) -> dict[str, list[tuple[int, int]]]:
        cfg = get_config().store
        out: dict[str, list[tuple[int, int]]] = {}
        rows = conn.execute(
            f"""SELECT IdentityID, HeartRate, CreateDate FROM {_HR_TABLE}
                WHERE CreateDate >= ? AND LENGTH(CAST(IdentityID AS TEXT)) >= 17""",
            (cfg.sqlite_min_time,),
        ).fetchall()
        for r in rows:
            dt = _parse_local(r["CreateDate"])
            if dt is None:
                continue
            aid = athlete_id_of(r["IdentityID"])
            out.setdefault(aid, []).append((_local_to_utc_ms(dt), int(r["HeartRate"])))
        for lst in out.values():
            lst.sort()
        return out

    def _load_wind(self, conn) -> dict[str, list[tuple[int, float, float]]]:
        cfg = get_config().store
        out: dict[str, list[tuple[int, float, float]]] = {}
        rows = conn.execute(
            f"""SELECT RegisterNum, WindSpeed, WindDirection, CreateTime FROM {_WIND_TABLE}
                WHERE CreateTime >= ? AND LENGTH(CAST(RegisterNum AS TEXT)) >= 17""",
            (cfg.sqlite_min_time,),
        ).fetchall()
        for r in rows:
            dt = _parse_local(r["CreateTime"])
            if dt is None:
                continue
            aid = athlete_id_of(r["RegisterNum"])
            out.setdefault(aid, []).append(
                (_local_to_utc_ms(dt), float(r["WindSpeed"]), _to_float(r["WindDirection"]) or 0.0))
        for lst in out.values():
            lst.sort()
        return out
