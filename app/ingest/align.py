# -*- coding: utf-8 -*-
"""对齐模块（D2）：把异构数据变成统一逐箭事实。

- 主键：弹着时间戳（每箭一条事实）
- 心率：该箭时刻前后 30s 内最近采样；风速：窗口 60s（可配）
- 对齐容错：找不到采样 → 置 NULL（不丢弃该箭）
- 缺测哨兵兜底：心率<=0、风速/风向<0 一律转 NULL（所有数据源，接口 v1.2 §十）
- mock 数据已自带逐箭心率/风速，直接透传；真 SQLite 三表联查走窗口匹配
- 输出：session_dim 摘要 + shot_fact 行（与 store 解耦，只产 dict）
"""
from __future__ import annotations

from statistics import fmean, pstdev

from app.ingest.base import SessionRaw, ShotRaw

HR_WINDOW_MS = 30_000
WIND_WINDOW_MS = 60_000


def _parse_utc_ms(iso: str) -> int | None:
    """ISO8601 UTC 毫秒（如 2026-08-03T09:00:00.123Z）→ epoch 毫秒。"""
    from datetime import datetime, timezone

    try:
        norm = iso.replace("Z", "+00:00") if iso.endswith(("Z", "z")) else iso
        dt = datetime.fromisoformat(norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def _nearest_sample(shot_ms: int, samples: list[tuple[int, float]], window_ms: int) -> float | None:
    """窗口内最近采样：取 |t_shot - t_sample| 最小且 <= window_ms 的样本值。"""
    best: tuple[int, float] | None = None
    for ts, val in samples:
        gap = abs(ts - shot_ms)
        if gap <= window_ms and (best is None or gap < abs(best[0] - shot_ms)):
            best = (ts, val)
    return best[1] if best else None


def _nearest_wind(shot_ms: int, samples: list[tuple[int, float, float]], window_ms: int) -> tuple[float, float] | None:
    """风速样本结构 (ms, speed, dir_deg)；返回窗口内最近一条的 (speed, dir)。"""
    best: tuple[int, float, float] | None = None
    for ts, speed, deg in samples:
        gap = abs(ts - shot_ms)
        if gap <= window_ms and (best is None or gap < abs(best[0] - shot_ms)):
            best = (ts, speed, deg)
    return (best[1], best[2]) if best else None


def _nearest_int(shot_ms: int, samples: list[tuple[int, int]], window_ms: int) -> int | None:
    best: tuple[int, int] | None = None
    for ts, val in samples:
        gap = abs(ts - shot_ms)
        if gap <= window_ms and (best is None or gap < abs(best[0] - shot_ms)):
            best = (ts, val)
    return best[1] if best else None


def _clean_hr(v):
    """缺测哨兵（v1.2 §十）：heartRate<=0 → None（不以 0 计入统计）。所有数据源在此兜底。"""
    return None if v is None or v <= 0 else v


def _clean_wind(v):
    """缺测哨兵：windSpeed/windDirection<0（-1）→ None；0 = 真实无风，保留。"""
    return None if v is None or v != v or v < 0 else v


def _mode_composition(shots: list[ShotRaw]) -> str:
    counts: dict[int, int] = {}
    for sh in shots:
        counts[sh.shooting_mode] = counts.get(sh.shooting_mode, 0) + 1
    names = {0: "试射", 1: "记分", 2: "同分", 3: "补射"}
    return "+".join(f"{names.get(k, str(k))}{v}" for k, v in sorted(counts.items()))


def align_session(session: SessionRaw, hr_samples: dict[str, list[tuple[int, int]]] | None = None,
                  wind_samples: dict[str, list[tuple[int, float]]] | None = None) -> tuple[dict, list[dict]]:
    """对齐一场训练 → (session_dim dict, shot_fact rows)。

    mock 场景 hr_samples/wind_samples 为 None → 直接透传逐箭值（mock 已对齐）。
    真数据场景传入 运动员→采样列表 的映射，按时间窗口匹配。
    """
    shots = session.shots
    hr_map = hr_samples or {}
    wind_map = wind_samples or {}

    fact_rows: list[dict] = []
    wind_vals: list[float] = []
    for idx, sh in enumerate(shots, start=1):
        shot_ms = _parse_utc_ms(sh.shot_time_utc)
        # 透传或窗口匹配
        if hr_samples is None:
            hr = sh.hr
        else:
            hr = _nearest_int(shot_ms, hr_map.get(session.athlete_id, []), HR_WINDOW_MS) if shot_ms is not None else None
        if wind_samples is None:
            wind_speed, wind_dir = sh.wind_speed, sh.wind_dir_deg
        else:
            nearest = _nearest_wind(shot_ms, wind_map.get(session.athlete_id, []), WIND_WINDOW_MS) if shot_ms is not None else None
            wind_speed, wind_dir = (nearest[0], nearest[1]) if nearest else (None, None)
        hr = _clean_hr(hr)
        wind_speed, wind_dir = _clean_wind(wind_speed), _clean_wind(wind_dir)
        if wind_speed is not None:
            wind_vals.append(wind_speed)
        fact_rows.append(
            {
                "athlete_id": sh.athlete_id,
                "session_id": sh.session_id,
                "shot_seq": idx,
                "score": sh.score,
                "hit": 1 if sh.hit else 0,
                "x_mm": sh.x_mm,
                "y_mm": sh.y_mm,
                "mcr_t": sh.mcr_t,
                "hr": hr,
                "wind_speed": wind_speed,
                "wind_dir_deg": wind_dir,
                "shooting_mode": sh.shooting_mode,
                "bow_type": sh.bow_type or "反曲弓",
                "video_ref": sh.video_ref,
                "shot_time_utc": sh.shot_time_utc,
                "shot_id": sh.shot_id,
                "score_id": sh.score_id,
                "lane": sh.lane,
                "release_time_utc": sh.release_time_utc,
                "hit_time_utc": sh.hit_time_utc,
                "flight_time_ms": sh.flight_time_ms,
                "inner_ten": None if sh.inner_ten is None else int(bool(sh.inner_ten)),
            }
        )

    session_time = shots[0].shot_time_utc if shots else ""
    dim = {
        "session_id": session.session_id,
        "athlete_id": session.athlete_id,
        "session_time_utc": session_time,
        "distance_m": session.distance_m,
        "shot_count": len(shots),
        "mode_composition": _mode_composition(shots),
        "avg_wind": round(fmean(wind_vals), 2) if wind_vals else None,
        "wind_stddev": round(pstdev(wind_vals), 2) if len(wind_vals) >= 2 else None,
        "site": session.site,
    }
    return dim, fact_rows
