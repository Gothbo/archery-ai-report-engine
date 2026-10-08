# -*- coding: utf-8 -*-
"""报告层 · 结构化结论（PR #4）：判定 verdicts / 风档差 / 和自己比 / 平台期。

这些都是「判定」，统一由引擎计算并写进报告正文 conclusions，门户只负责展示与措辞
（评估 §9）。引擎只给数值和代码，不在这里产出新的文案（文案要走禁语校验）。
口径：
- verdicts：沿用 rules.judge_metric（方向与阈值只读 config.mdc），delta 带符号（now − anchor）
- wind_gap：低风（< split）均环 − 高风（≥ split）均环，正数 = 风拉低了成绩；口径在 config.conclusions，
  默认值为演示口径（provisional=true），待 PM / 专家定
- self_compare：日报 = 时间上紧邻的前一场；其余粒度 = 紧邻的上一个窗口；直接从 shot_fact 现算
- plateau：每份已存正文的同粒度报告算一期（取它的 verdicts[metric]），按窗口时间排序，本期 + 之前 N−1 期
  全为 steady → triggered（修正旧逻辑「最近 3 行 report_memories = 上一份报告的 3 个指标」）
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import fmean

from app.config import EngineConfig
from app.metrics.performance import avg_score
from app.reports.window import window_bounds, window_of_shot
from app.store.database import Database

SCHEMA_VERSION = 1
METRIC_KEYS = ("avgScore", "mcrT", "dispersionMm", "hrVolatility")


# ---------------------------------------------------------------------------
# verdicts
# ---------------------------------------------------------------------------

def verdict_entry(cfg: EngineConfig, metric: str, value, anchor, *, verdict=None,
                  reason: str) -> dict:
    delta = round(value - anchor, 3) if value is not None and anchor is not None else None
    return {"verdict": verdict, "reason": reason, "value": value, "anchor": anchor,
            "delta": delta, "mdc": cfg.mdc[metric].threshold}


def reason_of(res: dict, comparable: bool) -> tuple[str | None, str]:
    """rules.judge_metric 结果 + 可比性 → (verdict, reason)。"""
    if not comparable:
        return None, "not_comparable"
    if res.get("degraded") or res.get("judge") is None:
        return None, "mdc_pending"
    if res["judge"] == "steady":
        return "steady", "below_mdc"
    return res["judge"], "above_mdc"


# ---------------------------------------------------------------------------
# wind_gap
# ---------------------------------------------------------------------------

def _valid_wind(ws) -> bool:
    return ws is not None and ws == ws and ws >= 0  # 缺测 / NaN / 负值哨兵（-1）不计入


def compute_wind_gap(cfg: EngineConfig, shots: list[dict]) -> dict:
    c = cfg.conclusions
    split = c.wind_gap_split_mps
    low = [s["score"] for s in shots if _valid_wind(s.get("wind_speed")) and s["wind_speed"] < split]
    high = [s["score"] for s in shots if _valid_wind(s.get("wind_speed")) and s["wind_speed"] >= split]
    if not low and not high:
        status = "no_wind_data"
    elif len(low) < c.wind_gap_min_shots or len(high) < c.wind_gap_min_shots:
        status = "insufficient_sample"
    else:
        status = "ok"
    gap = round(fmean(low) - fmean(high), 2) if status == "ok" else None
    return {
        "status": status,
        "triggered": bool(status == "ok" and gap is not None and gap >= c.wind_gap_threshold - 1e-9),
        "split_mps": split,
        "low": {"n_shots": len(low), "avg_score": round(fmean(low), 2) if low else None},
        "high": {"n_shots": len(high), "avg_score": round(fmean(high), 2) if high else None},
        "gap": gap,
        "min_shots": c.wind_gap_min_shots,
        "threshold": c.wind_gap_threshold,
        "provisional": c.provisional,
    }


# ---------------------------------------------------------------------------
# self_compare
# ---------------------------------------------------------------------------

def _majority(values: list):
    vals = [v for v in values if v is not None]
    return Counter(vals).most_common(1)[0][0] if vals else None


def _prev_window_key(cfg: EngineConfig, granularity: str, window_key: str) -> str | None:
    start_iso, _ = window_bounds(cfg, granularity, window_key)
    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    prev = (start - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return window_of_shot(cfg, prev, granularity)


def compute_self_compare(db: Database, cfg: EngineConfig, athlete_id: str, granularity: str,
                         window_key: str, session_id: str | None, metrics: dict,
                         shots: list[dict]) -> dict:
    out = {"status": "no_data", "previous": None, "current_avg_score": metrics["avg_score"],
           "delta": None, "direction": None, "comparable": None}
    if metrics["n_shots"] == 0:
        return out
    previous = None
    comparable = None
    if granularity == "daily":
        cur = db.query("SELECT * FROM session_dim WHERE session_id=?", (session_id,))
        if cur:
            cur = dict(cur[0])
            prev = db.query(
                """SELECT * FROM session_dim WHERE athlete_id=? AND session_time_utc<?
                   ORDER BY session_time_utc DESC LIMIT 1""", (athlete_id, cur["session_time_utc"]))
            if prev:
                prev = dict(prev[0])
                prev_shots = [dict(r) for r in db.shots_of_session(prev["session_id"])]
                if prev_shots:
                    prev_bow = _majority([s.get("bow_type") for s in prev_shots])
                    cur_bow = _majority([s.get("bow_type") for s in shots])
                    previous = {
                        "granularity": "daily", "window_key": f"daily:{prev['session_id']}",
                        "started_at_utc": prev["session_time_utc"], "n_shots": len(prev_shots),
                        "avg_score": round(avg_score([s["score"] for s in prev_shots]), 2),
                        "bow_type": prev_bow, "distance_m": prev.get("distance_m"),
                    }
                    comparable = (prev_bow == cur_bow and prev.get("distance_m") == cur.get("distance_m"))
    else:
        prev_key = _prev_window_key(cfg, granularity, window_key)
        if prev_key:
            start_iso, end_iso = window_bounds(cfg, granularity, prev_key)
            rows = db.query(
                """SELECT score FROM shot_fact
                   WHERE athlete_id=? AND shot_time_utc>=? AND shot_time_utc<?""",
                (athlete_id, start_iso, end_iso))
            scores = [r["score"] for r in rows]
            if scores:
                previous = {
                    "granularity": granularity, "window_key": prev_key, "started_at_utc": None,
                    "n_shots": len(scores), "avg_score": round(avg_score(scores), 2),
                    "bow_type": None, "distance_m": None,
                }
    if previous is None:
        out["status"] = "no_previous"
        return out
    delta = round(metrics["avg_score"] - previous["avg_score"], 2)
    out.update(status="ok", previous=previous, delta=delta,
               direction="higher" if delta > 0 else "lower" if delta < 0 else "same",
               comparable=comparable)
    return out


# ---------------------------------------------------------------------------
# plateau
# ---------------------------------------------------------------------------

def _window_start(db: Database, cfg: EngineConfig, granularity: str, window_key: str) -> str | None:
    """窗口在时间轴上的位置（UTC ISO，同粒度内可按字符串比较）。日报 = 场次时间。"""
    try:
        if granularity == "daily":
            rows = db.query("SELECT session_time_utc FROM session_dim WHERE session_id=?",
                            (window_key[6:] if window_key.startswith("daily:") else window_key,))
            return rows[0]["session_time_utc"] if rows else None
        return window_bounds(cfg, granularity, window_key)[0]
    except (ValueError, IndexError):
        return None


def compute_plateau(db: Database, cfg: EngineConfig, athlete_id: str, granularity: str,
                    window_key: str, report_id: str, verdicts: dict) -> dict:
    n = cfg.conclusions.plateau_periods
    metric = cfg.conclusions.plateau_metric
    current = {"window_key": window_key, "report_id": report_id,
               "verdict": (verdicts.get(metric) or {}).get("verdict")}
    cur_start = _window_start(db, cfg, granularity, window_key)
    candidates: list[tuple[str, dict]] = []
    if cur_start is not None:
        rows = db.query(
            """SELECT report_id, window_key, report_json FROM report_cache
               WHERE athlete_id=? AND granularity=? AND window_key<>? AND report_json IS NOT NULL
                 AND (mdc_version IS ? OR mdc_version=?)""",
            (athlete_id, granularity, window_key, cfg.mdc_source, cfg.mdc_source))
        for r in rows:
            start = _window_start(db, cfg, granularity, r["window_key"])
            if start is None or start >= cur_start:
                continue  # 只看本期之前的窗口（重新生成旧窗口时不会用到更新的窗口）
            try:
                body = json.loads(r["report_json"])
            except ValueError:
                continue
            v = (((body.get("conclusions") or {}).get("verdicts") or {}).get(metric) or {}).get("verdict")
            candidates.append((start, {"window_key": r["window_key"], "report_id": r["report_id"], "verdict": v}))
    candidates.sort(key=lambda x: x[0])
    recent = [c for _, c in candidates[-(n - 1):]] + [current]
    status = "ok" if len(recent) >= n else "insufficient_history"
    return {
        "status": status,
        "triggered": status == "ok" and all(r["verdict"] == "steady" for r in recent),
        "metric": metric,
        "periods": n,
        "recent": recent,
    }
