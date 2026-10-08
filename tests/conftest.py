# -*- coding: utf-8 -*-
"""pytest 共享夹具：隔离配置（临时 config.json + 临时 DB）+ 种子数据助手。

规则：测试一律不碰真实 facts.db；用 monkeypatch 重定向 ENGINE_CONFIG，
并在每个用例后清理 get_config lru_cache 与 Database 单例。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
REAL_CONFIG = BASE / "config.json"
ATHLETE = "1963169497552654337"  # mock 张明


def _make_temp_config(tmp_path: Path, *, mdc_source: str | None = "test-consensus-v1",
                      db_path: str | None = None) -> dict:
    cfg = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    cfg["store"]["db_path"] = str(db_path or (tmp_path / "facts.db"))
    cfg["store"]["sqlite_source_path"] = str(tmp_path / "real.db")
    cfg["mdc_source"] = mdc_source
    (tmp_path / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _reset_engine():
    import app.config as cfgmod
    import app.llm.tasks as tasksmod
    import app.store.database as dbmod
    cfgmod.get_config.cache_clear()
    dbmod._SINGLETON = None
    tasksmod.reset_registry()  # PR #3：LLM 单飞锁是进程内单例，用例间清空
    import app.llm.client as clientmod
    clientmod.reset_backend_probe_cache()  # PR #4：模型服务探测结果缓存，用例间清空


@pytest.fixture()
def engine_env(tmp_path, monkeypatch):
    """临时配置（mdc_source=test-consensus-v1，走判定路径）+ 重置引擎状态。"""
    cfg = _make_temp_config(tmp_path)
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("ENGINE_CONFIG", str(cfg_path))
    import app.config as cfgmod
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", cfg_path)  # 测试结束自动还原，防跨用例污染
    _reset_engine()
    yield cfg, str(tmp_path / "facts.db")
    _reset_engine()


@pytest.fixture()
def degraded_env(tmp_path, monkeypatch):
    """mdc_source=None：报告走降级模板（只描述不判定）。"""
    cfg = _make_temp_config(tmp_path, mdc_source=None)
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("ENGINE_CONFIG", str(cfg_path))
    import app.config as cfgmod
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", cfg_path)  # 测试结束自动还原，防跨用例污染
    _reset_engine()
    yield cfg, str(tmp_path / "facts.db")
    _reset_engine()


@pytest.fixture()
def db(engine_env):
    """临时 Database 实例（不经过单例）。"""
    from app.store.database import Database
    _, db_path = engine_env
    database = Database(db_path)
    yield database
    database.close()


@pytest.fixture()
def mock_db(db):
    """导入完整 mock 数据集的临时库（13 场 × 30 箭）。"""
    from app.ingest import ingest_source
    from app.ingest.mock_source import MockSource
    from app.memory.baseline import refresh_rolling_after_import
    ingest_source(db, MockSource(), on_imported=refresh_rolling_after_import)
    return db


# ---- 种子数据助手（直接写事实层，绕过 mock 文件）----

def seed_session(db, athlete_id: str, session_id: str, time_iso: str, scores: list[float],
                 *, hr: list[int] | None = None, wind: list[float] | None = None,
                 shooting_mode: int = 1, bow_type: str = "反曲弓", distance_m: int = 70,
                 x_mm: list[float] | None = None, y_mm: list[float] | None = None) -> None:
    """按统一契约插入一场训练（幂等）。"""
    from datetime import datetime, timedelta, timezone
    from app.ingest.align import align_session
    from app.ingest.base import SessionRaw, ShotRaw

    base = datetime.fromisoformat(time_iso.replace("Z", "+00:00"))
    shots = []
    for i, sc in enumerate(scores):
        shots.append(ShotRaw(
            athlete_id=athlete_id, session_id=session_id, shot_seq=i + 1,
            score=float(sc), hit=float(sc) > 0.0,
            x_mm=(x_mm[i] if x_mm else None), y_mm=(y_mm[i] if y_mm else None),
            mcr_t=0.40 + i * 0.001,
            hr=(hr[i] if hr else None),
            wind_speed=(wind[i] if wind else None), wind_dir_deg=90.0,
            shooting_mode=shooting_mode, bow_type=bow_type,
            shot_time_utc=(base + timedelta(seconds=20 * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        ))
    raw = SessionRaw(session_id=session_id, athlete_id=athlete_id, shots=shots, distance_m=distance_m)
    dim, rows = align_session(raw)
    db.upsert_session(dim)
    db.insert_shots(rows)


def seed_anchor(db, athlete_id: str, session_id: str, *, bow_type: str = "反曲弓") -> int:
    """基于某场次建立锚点，返回 snapshot_id。"""
    from app.memory.baseline import create_anchor_snapshot
    snap = create_anchor_snapshot(db, athlete_id, bow_type, session_id)
    return db.insert_baseline_snapshot(snap)
