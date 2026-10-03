# -*- coding: utf-8 -*-
"""v1.2 上行消息接收核心（与传输无关：HTTP 已接入，MQTT 订阅以后直接调用 ingest_messages 即可）。

处理流程（每条消息独立结果，互不影响）：
1. 校验：schema.json + 文档硬约束（validate.py）→ 失败 = invalid（记日志、不落库、不占用 messageId）
2. 去重：messageId 已受理 → duplicate（§4.3：仅处理一次，后续丢弃，不覆盖/不重算）
3. 分发（本期范围，开工包：dt 2/1/4/3/7）：
   - dt2 弹着 → v12_shot（shotId 已存在 → duplicate，不覆盖）
   - dt1 心率 / dt4 风 → v12_sample
   - dt3 视频 → v12_video（只存路径）
   - dt7 轨迹 → v12_trajectory（元数据 + 点列，不参与计算）
   - dt5 拉力 / dt6 撒放 → v12_raw_message（只存原文，不参与计算）
   - dt8 / dt9 → unsupported（不在本期范围；不落库、不占用 messageId，后续支持后可原样重发）
4. 重建：受影响的（运动员, 本地日）按现有聚类口径（同人同日、间隔 ≤ session_gap_minutes）
   重建 shot_fact/session_dim（场次号 {athlete_id}_{YYYYMMDD}_v{nn}，与 SQLite 源不冲突），
   失效相关日报缓存，并复用 refresh_rolling_after_import 刷新滚动基线。

结果 status：accepted | duplicate | invalid | unsupported。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import get_config
from app.ingest.align import align_session
from app.ingest.base import SessionRaw, ShotRaw
from app.ingest.v12.mapping import dt2_warnings, map_dt2, map_sample, utc_ms_str
from app.ingest.v12.validate import parse_utc_z, validate_message
from app.store.database import Database

logger = logging.getLogger("engine.ingest.v12")

IN_PHASE = {1, 2, 3, 4, 7}
RAW_ONLY = {5, 6}
SESSION_TAG = "v"  # 场次号后缀 _v01：与 SQLite 源 _01 区分

_LOCK = threading.Lock()  # Database 为单连接，接收端串行处理


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sha256(msg: dict) -> str:
    return hashlib.sha256(json.dumps(msg, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def ingest_messages(db: Database, messages: list[Any], on_imported=None) -> dict:
    """处理一批 v1.2 消息 → {"summary": {...}, "results": [...]}。"""
    with _LOCK:
        results: list[dict] = []
        affected: set[tuple[str, str]] = set()
        for idx, msg in enumerate(messages):
            res = _process_one(db, msg, affected)
            res["index"] = idx
            results.append(res)
        rebuilt = _rebuild(db, affected, on_imported)
    summary = {s: sum(1 for r in results if r["status"] == s)
               for s in ("accepted", "duplicate", "invalid", "unsupported")}
    summary["total"] = len(results)
    summary["sessions_rebuilt"] = rebuilt
    return {"summary": summary, "results": results}


def _process_one(db: Database, msg: Any, affected: set) -> dict:
    mid = msg.get("messageId") if isinstance(msg, dict) else None
    dt = msg.get("dataType") if isinstance(msg, dict) else None
    res: dict = {"messageId": mid if isinstance(mid, str) else None,
                 "dataType": dt if isinstance(dt, int) else None}
    errors = validate_message(msg)
    if errors:
        logger.warning("v1.2 消息结构非法，拒绝不落库 messageId=%s dataType=%s errors=%s", mid, dt, errors)
        return {**res, "status": "invalid", "reason": "结构非法（schema/文档硬约束）", "errors": errors}

    conn = db.conn
    seen = conn.execute("SELECT payload_sha256 FROM ingest_messages WHERE message_id=?", (mid,)).fetchone()
    if seen:
        out = {**res, "status": "duplicate", "reason": "messageId 已受理（§4.3：仅处理一次，不覆盖）"}
        if seen[0] != _sha256(msg):
            out["payload_differs"] = True  # 同 messageId 内容不同：按文档丢弃，但提示发送端
            logger.warning("v1.2 重复 messageId 内容不同（已丢弃）messageId=%s", mid)
        return out

    if dt not in IN_PHASE and dt not in RAW_ONLY:
        return {**res, "status": "unsupported",
                "reason": f"dataType={dt} 不在本期范围（本期 dt 1/2/3/4/7，dt 5/6 只存原文）；未落库，支持后可原样重发"}

    data = msg["data"]
    now = _now_utc()
    try:
        with conn:  # 单条消息原子：数据行 + messageId 同时提交
            outcome = _dispatch(conn, mid, dt, data, now, affected)
            if outcome.get("status") == "duplicate":
                conn.rollback()
                return {**res, **outcome}
            conn.execute(
                "INSERT INTO ingest_messages (message_id, data_type, payload_sha256, received_at_utc) VALUES (?,?,?,?)",
                (mid, dt, _sha256(msg), now))
    except Exception as exc:  # 存储异常：不占用 messageId，可重发
        logger.exception("v1.2 消息落库失败 messageId=%s", mid)
        return {**res, "status": "invalid", "reason": f"落库失败：{type(exc).__name__}", "errors": [str(exc)]}
    return {**res, "status": "accepted", **outcome}


def _dispatch(conn, mid: str, dt: int, data: dict, now: str, affected: set) -> dict:
    if dt == 2:
        row = map_dt2(data)
        dup = conn.execute("SELECT 1 FROM v12_shot WHERE shot_id=? OR (score_id IS NOT NULL AND score_id=?)",
                           (row["shot_id"], row["score_id"])).fetchone()
        if dup:
            return {"status": "duplicate", "reason": "shotId/scoreId 已入库（不覆盖，§4.3）"}
        row.update(message_id=mid, received_at_utc=now)
        cols = list(row)
        conn.execute(f"INSERT INTO v12_shot ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     tuple(row[c] for c in cols))
        affected.add((row["athlete_id"], row["local_date"]))
        out = {"athlete_id": row["athlete_id"], "shotId": row["shot_id"]}
        warns = dt2_warnings(data)
        if warns:
            out["warnings"] = warns
        return out

    if dt in (1, 4):
        row = map_sample(dt, data)
        row["message_id"] = mid
        cols = list(row)
        conn.execute(f"INSERT INTO v12_sample ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     tuple(row[c] for c in cols))
        _mark_shot(conn, row["shot_id"], affected)
        return {"hasShotId": row["shot_id"] is not None}

    if dt == 3:
        stored = 0
        for v in data["videos"]:
            cur = conn.execute(
                """INSERT OR IGNORE INTO v12_video
                     (shot_id, source, video_path, message_id, release_time_utc, shot_seq, lane)
                   VALUES (?,?,?,?,?,?,?)""",
                (v["shotId"], v["source"], v["videoPath"], mid, utc_ms_str(data["releaseTime"]),
                 data["shotSeq"], data.get("lane")))
            stored += cur.rowcount
            _mark_shot(conn, v["shotId"], affected)
        if stored == 0 and data["videos"]:
            return {"status": "duplicate", "reason": "该箭视频引用已存在（shotId+source，不覆盖）"}
        return {"videos_stored": stored}

    if dt == 7:
        cur = conn.execute(
            """INSERT OR IGNORE INTO v12_trajectory
                 (shot_id, message_id, release_time_utc, shot_seq, sample_rate_hz, point_count,
                  trajectory_length_mm, dispersion_mm, offset_x_mm, offset_y_mm, post_hold_ms,
                  stability_score, points_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data["shotId"], mid, utc_ms_str(data["releaseTime"]), data["shotSeq"], data["sampleRateHz"],
             len(data["points"]), data["trajectoryLength"], data["dispersionMm"], data["offsetX"],
             data["offsetY"], data["postHoldMs"], data["stabilityScore"], json.dumps(data["points"])))
        if cur.rowcount == 0:
            return {"status": "duplicate", "reason": "该箭轨迹已存在（shotId，不覆盖）"}
        return {"note": "轨迹已存储，本期不参与计算"}

    # dt5 / dt6：只存原文
    conn.execute(
        """INSERT INTO v12_raw_message (message_id, data_type, shot_id, release_time_utc, payload_json)
           VALUES (?,?,?,?,?)""",
        (mid, dt, data.get("shotId"), utc_ms_str(data.get("releaseTime")), json.dumps(data, ensure_ascii=False)))
    return {"note": "本期只存原文，不参与计算"}


def _mark_shot(conn, shot_id: str | None, affected: set) -> None:
    """实时流/事后通道晚于弹着到达：凭 shotId 找到所属（运动员, 本地日）以便重建。"""
    if not shot_id:
        return
    r = conn.execute("SELECT athlete_id, local_date FROM v12_shot WHERE shot_id=?", (shot_id,)).fetchone()
    if r:
        affected.add((r[0], r[1]))


# ---- 场次重建（复用现有 align/store/记忆层）----

def _ms(iso: str) -> int:
    return int(parse_utc_z(iso).timestamp() * 1000)


def _fallback_samples(conn, shot_id: str, anchor_ms: int) -> tuple[int | None, float | None, float | None]:
    """弹着快照缺测时，用同 shotId 的实时流样本兜底（取离锚点最近的非缺测值）。"""
    rows = conn.execute(
        "SELECT data_type, sample_time_utc, hr, wind_speed, wind_dir_deg FROM v12_sample WHERE shot_id=?",
        (shot_id,)).fetchall()
    hr = wind = wdir = None
    best_hr = best_wind = None
    for dt, ts, h, ws, wd in rows:
        gap = abs(_ms(ts) - anchor_ms)
        if dt == 1 and h is not None and (best_hr is None or gap < best_hr):
            best_hr, hr = gap, h
        if dt == 4 and ws is not None and (best_wind is None or gap < best_wind):
            best_wind, wind, wdir = gap, ws, wd
    return hr, wind, wdir


def _video_ref(conn, shot_id: str) -> str | None:
    rows = conn.execute("SELECT source, video_path FROM v12_video WHERE shot_id=?", (shot_id,)).fetchall()
    paths = {s: p for s, p in rows}
    return paths.get("top") or paths.get("side")


def _rebuild(db: Database, affected: set[tuple[str, str]], on_imported) -> int:
    if not affected:
        return 0
    store = get_config().store
    conn = db.conn
    rebuilt = 0
    touched_athletes: dict[str, list[str]] = {}
    for aid, day in sorted(affected):
        prefix = f"{aid}_{day}_{SESSION_TAG}"
        old_ids = [r[0] for r in conn.execute(
            "SELECT session_id FROM session_dim WHERE athlete_id=? AND session_id LIKE ?", (aid, prefix + "%"))]
        rows = conn.execute(
            "SELECT * FROM v12_shot WHERE athlete_id=? AND local_date=? ORDER BY shot_time_utc, shot_seq, shot_id",
            (aid, day)).fetchall()
        clusters: list[list] = []
        for r in rows:
            if clusters and _ms(r["shot_time_utc"]) - _ms(clusters[-1][-1]["shot_time_utc"]) <= \
                    store.session_gap_minutes * 60_000:
                clusters[-1].append(r)
            else:
                clusters.append([r])

        new_ids: list[str] = []
        with conn:
            for sid in old_ids:
                conn.execute("DELETE FROM shot_fact WHERE session_id=?", (sid,))
                conn.execute("DELETE FROM session_dim WHERE session_id=?", (sid,))
        for n, cluster in enumerate(clusters, start=1):
            sid = f"{prefix}{n:02d}"
            shots = []
            for i, r in enumerate(cluster, start=1):
                hr, ws, wd = r["hr"], r["wind_speed"], r["wind_dir_deg"]
                if hr is None or ws is None:
                    f_hr, f_ws, f_wd = _fallback_samples(conn, r["shot_id"], _ms(r["shot_time_utc"]))
                    hr = hr if hr is not None else f_hr
                    if ws is None and f_ws is not None:
                        ws, wd = f_ws, f_wd
                shots.append(ShotRaw(
                    athlete_id=aid, session_id=sid, shot_seq=i,
                    score=r["score"], hit=r["score"] > 0,
                    x_mm=r["x_mm"], y_mm=r["y_mm"], mcr_t=None,
                    hr=hr, wind_speed=ws, wind_dir_deg=wd,
                    shooting_mode=r["shooting_mode"], bow_type=r["bow_type"],
                    video_ref=_video_ref(conn, r["shot_id"]),
                    shot_time_utc=r["shot_time_utc"],
                    shot_id=r["shot_id"], score_id=r["score_id"], lane=r["lane"],
                    release_time_utc=r["release_time_utc"], hit_time_utc=r["hit_time_utc"],
                    flight_time_ms=r["flight_time_ms"],
                    inner_ten=None if r["inner_ten"] is None else bool(r["inner_ten"]),
                ))
            dim, fact_rows = align_session(SessionRaw(
                session_id=sid, athlete_id=aid, shots=shots,
                distance_m=store.default_distance_m, site="v1.2"))
            db.upsert_session(dim)
            db.insert_shots(fact_rows)
            new_ids.append(sid)
            rebuilt += 1
        for sid in set(old_ids) | set(new_ids):
            db.invalidate_session_cache(aid, sid)
        _ensure_profile(db, aid, rows[-1]["bow_type"] if rows else None)
        touched_athletes.setdefault(aid, []).extend(new_ids)
        logger.info("v1.2 重建 athlete=%s day=%s 场次 %d→%d 箭 %d", aid, day, len(old_ids), len(new_ids), len(rows))

    if on_imported is not None:
        for aid, sids in touched_athletes.items():
            try:
                on_imported(db, aid, sids)
            except Exception as exc:  # B7：记忆层异常只记日志，不阻断导入
                logger.warning("v1.2 导入后记忆更新失败（跳过）: %s", exc)
    return rebuilt


def _ensure_profile(db: Database, aid: str, bow_type: str | None) -> None:
    """dt9A 不在本期：首次出现的运动员建最小档案（展示名由脱敏 ID 派生，不含真实身份）。已有档案不覆盖。"""
    if db.get_profile(aid) is None:
        db.upsert_profile({"athlete_id": aid, "name": f"运动员{aid[-4:]}", "bow_type": bow_type})


__all__ = ["ingest_messages", "IN_PHASE", "RAW_ONLY"]

