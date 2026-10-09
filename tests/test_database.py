# -*- coding: utf-8 -*-
"""M4：数据库层测试（schema、幂等、缓存失效、软删、基线清理）。"""
import pytest

from app.store.database import Database


class TestSchema:
    def test_tables_created(self, db):
        rows = db._query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        names = {r["name"] for r in rows}
        assert {"session_dim", "shot_fact", "athlete_profile", "baseline_snapshots",
                "memory_notes", "report_memories", "report_cache"} <= names

    def test_reopen_existing_db(self, engine_env):
        # 重开同一路径不重建、不丢数据
        _, db_path = engine_env
        d1 = Database(db_path)
        d1.upsert_profile({"athlete_id": "A1", "name": "张三"})
        d1.close()
        d2 = Database(db_path)
        assert d2.get_profile("A1")["name"] == "张三"
        d2.close()


class TestFactLayer:
    def test_upsert_session_idempotent(self, db):
        db.upsert_session({"session_id": "S1", "athlete_id": "A1", "session_time_utc": "2026-08-03T00:00:00.000Z",
                           "distance_m": 70, "shot_count": 30, "mode_composition": "记分30"})
        db.upsert_session({"session_id": "S1", "athlete_id": "A1", "session_time_utc": "2026-08-03T00:00:00.000Z",
                           "distance_m": 70, "shot_count": 36, "mode_composition": "记分36"})
        assert db.session_exists("S1")
        assert db.session_of("S1")["shot_count"] == 36  # 覆盖

    def test_insert_shots_idempotent(self, db):
        row = {"athlete_id": "A1", "session_id": "S1", "shot_seq": 1, "score": 9.6, "hit": 1,
               "x_mm": 10.0, "y_mm": -5.0, "mcr_t": 0.4, "hr": 80, "wind_speed": 1.2, "wind_dir_deg": 90.0,
               "shooting_mode": 1, "bow_type": "反曲弓", "video_ref": None, "shot_time_utc": "2026-08-03T00:00:00.000Z"}
        assert db.insert_shots([row]) == 1
        assert db.insert_shots([row]) == 0  # UNIQUE(session_id, shot_seq) 跳过
        assert len(db.shots_of_session("S1")) == 1

    def test_missing_sentinels_null(self, db):
        row = {"athlete_id": "A1", "session_id": "S2", "shot_seq": 1, "score": 8.0, "hit": 0,
               "x_mm": None, "y_mm": None, "mcr_t": None, "hr": None, "wind_speed": None, "wind_dir_deg": None,
               "shooting_mode": 0, "bow_type": "反曲弓", "video_ref": None, "shot_time_utc": "2026-08-03T00:00:00.000Z"}
        db.insert_shots([row])
        shot = db.shots_of_session("S2")[0]
        assert shot["mcr_t"] is None and shot["hr"] is None and shot["x_mm"] is None


class TestMemoryLayer:
    def test_profile_upsert_and_get(self, db):
        db.upsert_profile({"athlete_id": "A1", "name": "张明", "bow_type": "反曲弓"})
        p = db.get_profile("A1")
        assert p["name"] == "张明"
        db.upsert_profile({"athlete_id": "A1", "name": "张明改", "age": 22})
        assert db.get_profile("A1")["name"] == "张明改"

    def test_note_soft_delete_audit(self, db):
        nid = db.insert_note({"athlete_id": "A1", "note_type": "injury", "content": "右肩痛",
                              "author_role": "athlete", "actor_id": "A1", "created_at_utc": "2026-09-01T00:00:00.000Z"})
        assert db.close_note(nid, "A1") is True
        assert db.close_note(nid, "A1") is False  # 已关闭
        assert db.close_note(nid, "OTHER") is False  # 归属校验
        assert len(db.list_notes("A1", status="active")) == 0
        closed = db._query("SELECT status FROM memory_notes WHERE id=?", (nid,))
        assert closed[0]["status"] == "closed"

    def test_baseline_snapshot_prune(self, db):
        for i in range(5):
            db.insert_baseline_snapshot({"athlete_id": "A1", "bow_type": "反曲弓", "snap_type": "rolling",
                                         "n_shots": 30, "avg_score": 9.0 + i * 0.1,
                                         "collected_at_utc": f"2026-08-0{i + 1}T00:00:00.000Z"})
        db.insert_baseline_snapshot({"athlete_id": "A1", "bow_type": "反曲弓", "snap_type": "anchor",
                                     "n_shots": 30, "avg_score": 9.0,
                                     "collected_at_utc": "2026-08-01T00:00:00.000Z"})
        db.prune_rolling_snapshots("A1", keep=3)
        rolls = db.list_snapshots("A1", snap_type="rolling")
        assert len(rolls) == 3
        anchors = db.list_snapshots("A1", snap_type="anchor")
        assert len(anchors) == 1  # 锚点永不清除

    def test_report_cache_mdc_version(self, db):
        db.put_cached_report("R1", "A1", "weekly", "weekly:2026-W32", "v1")
        assert db.get_cached_report("A1", "weekly", "weekly:2026-W32", "v1") == "R1"
        assert db.get_cached_report("A1", "weekly", "weekly:2026-W32", "v2") is None  # 版本不串

    def test_invalidate_daily_cache(self, db):
        db.put_cached_report("R1", "A1", "daily", "daily:S001", None)
        db.invalidate_session_cache("A1", "S001")
        assert db.get_cached_report("A1", "daily", "daily:S001", None) is None
