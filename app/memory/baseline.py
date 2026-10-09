# -*- coding: utf-8 -*-
"""记忆层 · 基线快照（双参照系，拍板 C3/D2）。

- rolling：导入成功后重算，口径 = 接口 v1.2 §9B 最近 N 支记分箭（shootingMode=1），
  按 (athlete_id, bow_type) 分组；未满 N 支 → n_shots 标记（报告层降级文案）；
  保留策略：仅保留最近 keep 条滚动快照（B8，锚点永不清除）
- anchor：人工触发建立（API 提供端点），携带可比性元数据
  （source_session_id / distance_m / mode_composition / avg_wind，A3）
- 指标：复用 metrics 纯函数（avgScore / mcrT / hrVolatility / dispersionMm），
  单指标缺测存 NULL 而非 0（B11）
- MDC 判定只针对锚点与上一期；滚动只描述水平（D3）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import get_config
from app.metrics.performance import avg_score
from app.metrics.physiology import hr_volatility
from app.metrics.process import dispersion_mm, mean_mcr_t
from app.store.database import Database

logger = logging.getLogger("engine.memory.baseline")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _shots_as_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def compute_rolling_snapshot(db: Database, athlete_id: str, bow_type: str) -> dict | None:
    """重算滚动基线：最近 N 支记分箭（shootingMode=1）按时间倒序取。"""
    cfg = get_config()
    n = cfg.rolling_window_shots
    rows = db.query(
        """SELECT * FROM shot_fact
           WHERE athlete_id=? AND bow_type=? AND shooting_mode=1
           ORDER BY shot_time_utc DESC LIMIT ?""",
        (athlete_id, bow_type, n),
    )
    shots = _shots_as_dicts(rows)
    if not shots:
        return None
    snap = _snapshot_values(athlete_id, bow_type, "rolling", shots)
    snap["source_session_id"] = None
    snap["distance_m"] = None
    snap["mode_composition"] = None
    snap["avg_wind"] = None
    return snap


def create_anchor_snapshot(db: Database, athlete_id: str, bow_type: str, source_session_id: str) -> dict:
    """建立锚点（赛季初/入队测试/换弓种）：取来源场次的记分箭，并落可比性元数据（A3）。"""
    cfg = get_config()
    session_rows = db.query(
        """SELECT * FROM shot_fact WHERE session_id=? AND shooting_mode=1""",
        (source_session_id,),
    )
    shots = _shots_as_dicts(session_rows)
    if not shots:
        raise ValueError(f"来源场次 {source_session_id} 无记分箭，锚点无法建立")

    dim_rows = db.query("SELECT * FROM session_dim WHERE session_id=?", (source_session_id,))
    dim = dict(dim_rows[0]) if dim_rows else {}
    snap = _snapshot_values(athlete_id, bow_type, "anchor", shots)
    snap["source_session_id"] = source_session_id
    snap["distance_m"] = dim.get("distance_m")
    snap["mode_composition"] = dim.get("mode_composition")
    snap["avg_wind"] = dim.get("avg_wind")
    if not snap["distance_m"] or not snap["mode_composition"]:
        raise ValueError("锚点场次缺少可比性元数据（distance_m/mode_composition），A3 要求必填")
    return snap


def _snapshot_values(athlete_id: str, bow_type: str, snap_type: str, shots: list[dict]) -> dict:
    """从箭集计算指标快照（复用 metrics 纯函数；单指标缺测存 NULL 而非 0）。"""
    cfg = get_config()
    mdc = cfg.mdc
    snap: dict = {
        "athlete_id": athlete_id,
        "bow_type": bow_type,
        "snap_type": snap_type,
        "n_shots": len(shots),
        "collected_at_utc": _now_utc(),
    }
    if "avgScore" in mdc and shots:
        snap["avg_score"] = round(avg_score([s["score"] for s in shots]), 3)
    else:
        snap["avg_score"] = None

    mcr_vals = [s["mcr_t"] for s in shots if s.get("mcr_t") is not None]
    snap["mcr_t"] = round(mean_mcr_t(mcr_vals), 3) if mcr_vals else None

    hr_vals = [s["hr"] for s in shots if s.get("hr") is not None]
    snap["hr_volatility"] = round(hr_volatility(hr_vals), 1) if hr_vals else None

    xs = [s["x_mm"] for s in shots if s.get("x_mm") is not None and s.get("y_mm") is not None]
    ys = [s["y_mm"] for s in shots if s.get("x_mm") is not None and s.get("y_mm") is not None]
    snap["dispersion_mm"] = round(dispersion_mm(xs, ys), 1) if len(xs) >= 2 else None
    return snap


def refresh_rolling_after_import(db: Database, athlete_id: str, session_ids: list[str]) -> None:
    """导入成功回调：重算滚动基线 + 清理旧滚动快照（B8）。"""
    cfg = get_config()
    # 取该运动员出现过的弓种（当前以档案为主，缺少则用事实表 distinct）
    bow_types = [r["bow_type"] for r in db.query(
        "SELECT DISTINCT bow_type FROM shot_fact WHERE athlete_id=?", (athlete_id,)
    )]
    for bow in bow_types:
        snap = compute_rolling_snapshot(db, athlete_id, bow)
        if snap:
            db.insert_baseline_snapshot(snap)
            db.prune_rolling_snapshots(athlete_id, cfg.memory.rolling_snapshot_keep)
            logger.info("滚动基线已刷新 athlete=%s bow=%s n=%d", athlete_id, bow, snap["n_shots"])
