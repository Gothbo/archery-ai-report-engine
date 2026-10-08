# -*- coding: utf-8 -*-
"""PR #4：报告正文落库 + 只读正文接口 + 问答/指导复用已存正文 + 视角剔除 + 未知场次 404。"""
import json

import pytest
from fastapi.testclient import TestClient

import tests.conftest as ct
from app.main import app
from app.memory.notes import add_note
from app.store.database import get_database
from tests.conftest import ATHLETE, seed_anchor

BASE = f"/api/v1/athletes/{ATHLETE}"


@pytest.fixture()
def client(engine_env):
    c = TestClient(app)
    c.post("/api/v1/ingest/mock")
    db = get_database()
    sid = db.query("SELECT session_id FROM session_dim ORDER BY session_time_utc LIMIT 1")[0]["session_id"]
    seed_anchor(db, ATHLETE, sid)
    return c


def _weekly(c, view="coach", refresh=False, week="2026-W33"):
    params = {"week": week, "view": view}
    if refresh:
        params["refresh"] = "1"
    r = c.post(f"{BASE}/reports/weekly", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _memory_ids():
    return [r["id"] for r in get_database().query("SELECT id FROM report_memories ORDER BY id")]


class TestStoredBody:
    def test_body_persisted_without_notes_section(self, client):
        add_note(get_database(), ATHLETE, "goal", "冲击 9.8", "athlete", "A1")
        rep = _weekly(client)
        assert any(s["key"] == "notes" for s in rep["sections"])  # 返回里有备注
        row = get_database().get_report_row(rep["report_id"])
        stored = json.loads(row["report_json"])
        assert stored["report_id"] == rep["report_id"]
        assert not any(s["key"] == "notes" for s in stored["sections"])  # 落库正文不含备注

    def test_cache_hit_returns_stored_body(self, client):
        r1 = _weekly(client)
        r2 = _weekly(client)
        assert r1["cached"] is False and r2["cached"] is True
        assert r2["report_id"] == r1["report_id"]
        assert r2["generated_at_utc"] == r1["generated_at_utc"]
        assert r2["sections"] == r1["sections"]
        assert r2["conclusions"] == r1["conclusions"]

    def test_list_reports_hides_report_json(self, client):
        _weekly(client)
        rows = client.get(f"{BASE}/reports").json()["reports"]
        assert rows and all("report_json" not in r for r in rows)
        assert set(rows[0]) == {"report_id", "athlete_id", "granularity", "window_key",
                                "mdc_version", "generated_at_utc"}


class TestContentEndpoint:
    def test_read_only_same_body(self, client):
        rep = _weekly(client)
        mems = _memory_ids()
        cache_ts = get_database().get_report_row(rep["report_id"])["generated_at_utc"]
        for _ in range(2):
            g = client.get(f"/api/v1/reports/{rep['report_id']}/content", params={"view": "coach"})
            assert g.status_code == 200
            body = g.json()
            assert body["report_id"] == rep["report_id"] and body["cached"] is True
            assert body["view"] == "coach"
            assert body["generated_at_utc"] == rep["generated_at_utc"]
            assert body["sections"] == rep["sections"]
            assert body["metrics"] == rep["metrics"] and body["conclusions"] == rep["conclusions"]
        assert _memory_ids() == mems  # 不重写历史结论
        assert get_database().get_report_row(rep["report_id"])["generated_at_utc"] == cache_ts

    def test_athlete_view_strips_coach_extra_and_coach_notes(self, client):
        rep = _weekly(client, view="coach")
        assert "coach_extra" in rep
        add_note(get_database(), ATHLETE, "coach_note", "私密教练观察", "coach", "C1")
        add_note(get_database(), ATHLETE, "injury", "右肩酸", "athlete", "A1")
        rid = rep["report_id"]
        a = client.get(f"/api/v1/reports/{rid}/content", params={"view": "athlete"}).json()
        c = client.get(f"/api/v1/reports/{rid}/content", params={"view": "coach"}).json()
        assert "coach_extra" not in a and a["view"] == "athlete"
        assert "coach_extra" in c and "rolling_baseline" in c["coach_extra"]
        ta = json.dumps(a["sections"], ensure_ascii=False)
        tc = json.dumps(c["sections"], ensure_ascii=False)
        assert "私密教练观察" not in ta and "右肩酸" in ta
        assert "私密教练观察" in tc  # 生成后新增的备注也能现取到

    def test_default_view_is_athlete(self, client):
        rep = _weekly(client, view="coach")
        body = client.get(f"/api/v1/reports/{rep['report_id']}/content").json()
        assert body["view"] == "athlete" and "coach_extra" not in body

    def test_generate_athlete_view_strips_coach_extra(self, client):
        rep = _weekly(client, view="athlete", refresh=True)
        assert "coach_extra" not in rep
        hit = _weekly(client, view="athlete")
        assert hit["cached"] is True and "coach_extra" not in hit
        coach = _weekly(client, view="coach")
        assert coach["cached"] is True and "coach_extra" in coach  # 落库正文保留 coach_extra

    def test_bad_view_422(self, client):
        rep = _weekly(client)
        assert client.get(f"/api/v1/reports/{rep['report_id']}/content",
                          params={"view": "admin"}).status_code == 422

    def test_unknown_id_404(self, client):
        r = client.get("/api/v1/reports/nope/content")
        assert r.status_code == 404 and r.json()["code"] == "REPORT-NOT-FOUND"

    def test_regenerated_old_id_404(self, client):
        old = _weekly(client)["report_id"]
        _weekly(client, refresh=True)
        r = client.get(f"/api/v1/reports/{old}/content")
        assert r.status_code == 404 and r.json()["code"] == "REPORT-NOT-FOUND"

    def test_legacy_row_without_body(self, client):
        db = get_database()
        db.put_cached_report("LEGACY-1", ATHLETE, "weekly", "2026-W34", "test-consensus-v1")
        r = client.get("/api/v1/reports/LEGACY-1/content")
        assert r.status_code == 404 and r.json()["code"] == "REPORT-BODY-MISSING"
        # 生成接口（不带 refresh）命中旧行：按未命中重新计算并补存正文，report_id 不变（真引擎联调 #2）
        hit = client.post(f"{BASE}/reports/weekly", params={"week": "2026-W34"}).json()
        assert hit["report_id"] == "LEGACY-1" and hit["cached"] is False and "sections" in hit
        assert client.get("/api/v1/reports/LEGACY-1/content").status_code == 200
        again = client.post(f"{BASE}/reports/weekly", params={"week": "2026-W34"}).json()
        assert again["report_id"] == "LEGACY-1" and again["cached"] is True
        # 问答复用补存后的正文
        a = client.post(f"{BASE}/ask", json={"question": "怎么样", "granularity": "weekly",
                                             "window_key": "2026-W34", "view": "coach"})
        assert a.status_code == 200
        row = db.get_cached_report_row(ATHLETE, "weekly", "2026-W34", "test-consensus-v1")
        assert row["report_id"] == "LEGACY-1" and row["report_json"]

    def test_body_missing_regenerate_keeps_id_and_memories(self, client):
        """联调复现：日报正文被置空（模拟 PR #4 之前的旧行）→ 不带 refresh 生成要补存正文，
        且不能像 refresh=1 那样清掉 / 重写该报告的历史结论。"""
        db = get_database()
        sid = db.query("SELECT session_id FROM session_dim WHERE athlete_id=? ORDER BY session_time_utc DESC "
                       "LIMIT 1", (ATHLETE,))[0]["session_id"]
        params = {"session_id": sid, "view": "coach"}
        rep = client.post(f"{BASE}/reports/daily", params=params).json()
        rid = rep["report_id"]
        mems = db.query("SELECT * FROM report_memories WHERE report_id=? ORDER BY id", (rid,))
        assert mems  # 有锚点、样本达标 → 生成时写了历史结论
        mems = [dict(m) for m in mems]
        db._execute("UPDATE report_cache SET report_json=NULL WHERE report_id=?", (rid,))
        r = client.get(f"/api/v1/reports/{rid}/content", params={"view": "coach"})
        assert r.status_code == 404 and r.json()["code"] == "REPORT-BODY-MISSING"

        regen = client.post(f"{BASE}/reports/daily", params=params).json()
        assert regen["report_id"] == rid and regen["cached"] is False
        assert regen["metrics"] == rep["metrics"] and regen["conclusions"]["verdicts"] == rep["conclusions"]["verdicts"]
        body = client.get(f"/api/v1/reports/{rid}/content", params={"view": "coach"})
        assert body.status_code == 200 and body.json()["report_id"] == rid
        assert body.json()["generated_at_utc"] == regen["generated_at_utc"] == \
            db.get_report_row(rid)["generated_at_utc"]
        assert [dict(m) for m in db.query("SELECT * FROM report_memories WHERE report_id=? ORDER BY id",
                                          (rid,))] == mems  # 历史结论原样保留（不删、不重写、不重复）
        hit = client.post(f"{BASE}/reports/daily", params=params).json()
        assert hit["report_id"] == rid and hit["cached"] is True

        # 对照：refresh=1 才会换 id 并清掉旧报告的历史结论
        fresh = client.post(f"{BASE}/reports/daily", params={**params, "refresh": "1"}).json()
        assert fresh["report_id"] != rid
        assert not db.query("SELECT 1 FROM report_memories WHERE report_id=?", (rid,))


class TestAskReusesStoredBody:
    def test_ask_keeps_report_id_and_memories(self, client):
        rep = _weekly(client)
        mems = _memory_ids()
        for _ in range(2):
            a = client.post(f"{BASE}/ask", json={"question": "这周怎么样", "granularity": "weekly",
                                                 "window_key": "2026-W33", "view": "coach"})
            assert a.status_code == 200
        assert client.get(f"/api/v1/reports/{rep['report_id']}").status_code == 200  # 以前这里 404
        body = client.get(f"/api/v1/reports/{rep['report_id']}/content", params={"view": "coach"}).json()
        assert body["generated_at_utc"] == rep["generated_at_utc"]
        assert _memory_ids() == mems

    def test_ask_context_version_stable(self, client):
        _weekly(client)
        q = {"question": "这周怎么样", "granularity": "weekly", "window_key": "2026-W33", "view": "coach"}
        v1 = client.post(f"{BASE}/ask", json=q).json()["context_version"]
        v2 = client.post(f"{BASE}/ask", json=q).json()["context_version"]
        assert v1 == v2

    def test_ask_without_cache_generates(self, client):
        a = client.post(f"{BASE}/ask", json={"question": "怎么样", "granularity": "weekly",
                                             "window_key": "2026-W32", "view": "coach"})
        assert a.status_code == 200
        assert get_database().get_cached_report_row(ATHLETE, "weekly", "2026-W32", "test-consensus-v1")


class TestGuidanceReusesStoredBody:
    def test_guidance_report_id_stable(self, tmp_path, monkeypatch):
        from tests.test_guidance_stream import _write_cfg, demo_mock, parse_sse, use_mock
        _write_cfg(tmp_path, monkeypatch)
        try:
            c = TestClient(app)
            c.post("/api/v1/ingest/mock")
            rep = _weekly(c, view="coach", refresh=True)
            use_mock(monkeypatch, demo_mock())
            ids, versions = [], []
            for _ in range(2):
                r = c.post(f"{BASE}/guidance/stream",
                           json={"granularity": "weekly", "window_key": "2026-W33", "view": "coach"})
                assert r.status_code == 200
                acc = dict(parse_sse(r.text))["accepted"]
                ids.append(acc["report_id"])
                versions.append(acc["context_version"])
            assert ids == [rep["report_id"]] * 2
            assert versions[0] == versions[1]
            assert c.get(f"/api/v1/reports/{rep['report_id']}/content").status_code == 200
        finally:
            ct._reset_engine()

    def test_guidance_unknown_session_404_releases_lock(self, tmp_path, monkeypatch):
        from tests.test_guidance_stream import _write_cfg
        _write_cfg(tmp_path, monkeypatch)
        try:
            c = TestClient(app)
            c.post("/api/v1/ingest/mock")
            r = c.post(f"{BASE}/guidance/stream",
                       json={"granularity": "daily", "window_key": "daily:NOPE", "view": "coach"})
            assert r.status_code == 404 and r.json()["code"] == "SESSION-NOT-FOUND"
            assert c.get("/api/v1/llm/status").json()["busy"] is False
        finally:
            ct._reset_engine()


class TestUnknownSession:
    def test_daily_unknown_session_404_no_cache(self, client):
        r = client.post(f"{BASE}/reports/daily", params={"session_id": "daily:S001"})
        assert r.status_code == 404 and r.json()["code"] == "SESSION-NOT-FOUND"
        n = get_database().query("SELECT COUNT(*) c FROM report_cache WHERE window_key LIKE 'daily:daily:%'")[0]["c"]
        assert n == 0

    def test_daily_session_of_other_athlete_404(self, client):
        r = client.post("/api/v1/athletes/OTHER/reports/daily", params={"session_id": "S001"})
        assert r.status_code == 404 and r.json()["code"] == "SESSION-NOT-FOUND"

    def test_daily_known_session_ok(self, client):
        r = client.post(f"{BASE}/reports/daily", params={"session_id": "S001"})
        assert r.status_code == 200 and r.json()["metrics"]["n_shots"] > 0

    def test_ask_unknown_daily_window_404(self, client):
        r = client.post(f"{BASE}/ask", json={"question": "怎么样", "granularity": "daily",
                                             "window_key": "daily:NOPE"})
        assert r.status_code == 404 and r.json()["code"] == "SESSION-NOT-FOUND"
