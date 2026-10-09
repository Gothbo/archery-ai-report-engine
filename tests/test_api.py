# -*- coding: utf-8 -*-
"""M4：API 测试（FastAPI TestClient 全链路：health/ingest/报告/备注/档案/基线）。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import ATHLETE


@pytest.fixture()
def client(engine_env, monkeypatch):
    from app.store.database import get_database
    return TestClient(app)


class TestHealth:
    def test_health(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["timezone"] == "Asia/Shanghai"
        assert body["mdc_source"] == "test-consensus-v1"


class TestIngestApi:
    def test_ingest_mock_and_reimport_idempotent(self, client):
        r1 = client.post("/api/v1/ingest/mock")
        assert r1.status_code == 200
        assert r1.json()["stats"]["inserted_shots"] == 390
        r2 = client.post("/api/v1/ingest/mock")
        assert r2.status_code == 200
        assert r2.json()["stats"]["inserted_shots"] == 0  # 幂等

    def test_ingest_mock_missing_file_404(self, client):
        r = client.post("/api/v1/ingest/mock", params={"mock_path": "C:/nope/not_exist.json"})
        assert r.status_code == 404


class TestReportsApi:
    def test_generate_and_get(self, client):
        client.post("/api/v1/ingest/mock")
        sid = "S001"
        r = client.post("/api/v1/athletes/1963169497552654337/reports/daily", params={"session_id": sid})
        assert r.status_code == 200
        report_id = r.json()["report_id"]
        g = client.get(f"/api/v1/reports/{report_id}")
        assert g.status_code == 200

    def test_daily_requires_session_id(self, client):
        r = client.post("/api/v1/athletes/1963169497552654337/reports/daily")
        assert r.status_code == 400

    def test_bad_granularity_400(self, client):
        r = client.post("/api/v1/athletes/1963169497552654337/reports/decade")
        assert r.status_code == 400

    def test_weekly_requires_week(self, client):
        r = client.post("/api/v1/athletes/1963169497552654337/reports/weekly")
        assert r.status_code == 400

    def test_list_reports(self, client):
        client.post("/api/v1/ingest/mock")
        client.post("/api/v1/athletes/1963169497552654337/reports/daily", params={"session_id": "S001"})
        r = client.get("/api/v1/athletes/1963169497552654337/reports")
        assert r.status_code == 200
        assert len(r.json()["reports"]) == 1

    def test_get_missing_report_404(self, client):
        r = client.get("/api/v1/reports/nonexistent-id")
        assert r.status_code == 404

    def test_view_validation(self, client):
        r = client.post("/api/v1/athletes/1963169497552654337/reports/weekly",
                        params={"week": "2026-W32", "view": "admin"})
        assert r.status_code == 422


class TestNotesApi:
    def test_note_crud_and_ownership(self, client):
        aid = ATHLETE
        c = client.post("/api/v1/athletes/1963169497552654337/notes",
                        json={"note_type": "injury", "content": "右肩酸痛", "author_role": "athlete",
                              "actor_id": aid})
        assert c.status_code == 200
        note_id = c.json()["note_id"]
        # 归属校验：他人路径删不到
        r = client.delete(f"/api/v1/athletes/OTHER/notes/{note_id}")
        assert r.status_code == 404
        r2 = client.delete(f"/api/v1/athletes/1963169497552654337/notes/{note_id}")
        assert r2.status_code == 200

    def test_note_validation(self, client):
        r = client.post("/api/v1/athletes/1963169497552654337/notes",
                        json={"note_type": "bogus", "content": "x", "author_role": "athlete", "actor_id": "a"})
        assert r.status_code == 400

    def test_notes_view_filter(self, client):
        client.post("/api/v1/athletes/1963169497552654337/notes",
                    json={"note_type": "coach_note", "content": "内部观察", "author_role": "coach", "actor_id": "c1"})
        r_a = client.get("/api/v1/athletes/1963169497552654337/notes", params={"view": "athlete"})
        r_c = client.get("/api/v1/athletes/1963169497552654337/notes", params={"view": "coach"})
        assert all(n["note_type"] != "coach_note" for n in r_a.json()["notes"])
        assert any(n["note_type"] == "coach_note" for n in r_c.json()["notes"])


class TestAthleteApi:
    def test_profile_upsert_get(self, client):
        r = client.put("/api/v1/athletes/1963169497552654337/profile",
                       json={"name": "张明", "age": 22, "bow_type": "反曲弓"})
        assert r.status_code == 200
        g = client.get("/api/v1/athletes/1963169497552654337/profile")
        assert g.json()["name"] == "张明"

    def test_profile_missing_404(self, client):
        r = client.get("/api/v1/athletes/nobody/profile")
        assert r.status_code == 404

    def test_sessions_list(self, client):
        client.post("/api/v1/ingest/mock")
        r = client.get("/api/v1/athletes/1963169497552654337/sessions")
        assert len(r.json()["sessions"]) == 13

    def test_baseline_list_and_anchor(self, client):
        client.post("/api/v1/ingest/mock")
        r = client.post("/api/v1/athletes/1963169497552654337/baseline/anchor",
                        json={"bow_type": "反曲弓", "source_session_id": "S001"})
        assert r.status_code == 200
        bl = client.get("/api/v1/athletes/1963169497552654337/baseline")
        snaps = bl.json()["snapshots"]
        assert any(s["snap_type"] == "anchor" for s in snaps)

    def test_anchor_missing_session_400(self, client):
        client.post("/api/v1/ingest/mock")
        r = client.post("/api/v1/athletes/1963169497552654337/baseline/anchor",
                        json={"bow_type": "反曲弓", "source_session_id": "S999"})
        assert r.status_code == 400


class TestSessionApi:
    def test_session_detail(self, client):
        client.post("/api/v1/ingest/mock")
        r = client.get("/api/v1/sessions/S001")
        assert r.status_code == 200
        assert len(r.json()["shots"]) == 30

    def test_session_missing_404(self, client):
        r = client.get("/api/v1/sessions/S999")
        assert r.status_code == 404
