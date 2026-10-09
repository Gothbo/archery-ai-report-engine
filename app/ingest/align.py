# -*- coding: utf-8 -*-
"""对齐模块（D2）：把异构数据变成统一逐箭事实。

- 主键：弹着时间戳（每箭一条事实）
- 心率：该箭时刻前后 30s 内最近采样；风速：窗口 60s（可配）
- 对齐容错：找不到采样 → 置 NULL（不丢弃该箭）
- mock 数据已自带逐箭心率/风速，直接透传；真 SQLite 三表联查走窗口匹配
- 输出：session_dim 摘要 + shot_fact 行（与 store 解耦，只产 dict）
"""
from __future__ import annotations

from statistics import fmean, pstdev

from app.ingest.base import SessionRaw, ShotRaw
from app.timeutil import parse_iso_utc_ms

HR_WINDOW_MS = 30_000
WIND_WINDOW_MS = 60_000


def nearest_in_window(shot_ms: int, samples: list[tuple], window_ms: int) -> tuple | None:
    """窗口内时间最近样本：取 |t_shot - t_sample| 最小且 <= window_ms 的一条，无则 None。

    samples 为 (ms, ...) 元组列表，按首个元素比较；并列取先出现者（时间序稳定）。
    对齐口径唯一来源：HR(30s)/风(60s) 及逐箭匹配都走这里，避免多处实现漂移。
    """
    best: tuple | None = None
    best_gap: int | None = None
    for s in samples:
        gap = abs(s[0] - shot_ms)
        if gap <= window_ms and (best_gap is None or gap < best_gap):
            best, best_gap = s, gap
    return best


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
        shot_ms = parse_iso_utc_ms(sh.shot_time_utc)
        # 透传或窗口匹配
        if hr_samples is None:
            hr = sh.hr
        else:
            hr_hit = nearest_in_window(shot_ms, hr_map.get(session.athlete_id, []), HR_WINDOW_MS) if shot_ms is not None else None
            hr = hr_hit[1] if hr_hit else None
        if wind_samples is None:
            wind_speed, wind_dir = sh.wind_speed, sh.wind_dir_deg
        else:
            wind_hit = nearest_in_window(shot_ms, wind_map.get(session.athlete_id, []), WIND_WINDOW_MS) if shot_ms is not None else None
            wind_speed, wind_dir = (wind_hit[1], wind_hit[2]) if wind_hit else (None, None)
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
