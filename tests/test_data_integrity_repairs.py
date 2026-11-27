# -*- coding: utf-8 -*-
"""数据一致性修复回归：库实例标识 / 周期窗口快照 / v1.2 原子重建与恢复。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.reports.window import WindowKey
from tests.test_desktop_ingest import URL as DESKTOP_URL, _shot, _snapshot
from tests.test_v12_ingest import URL as V12_URL, make_dt2

DAY_A = "2026-11-19"
DAY_B = "2026-11-20"
SHOT_DAY = "2026-09-07"
V12_LOCAL_DAY = "20260907"


def _client(engine_env):
    import app.main as main
    return TestClient(main.app)


def _db():
    from app.store.database import get_database
    return get_database()


def _week_key() -> str:
    from app.config import get_config
    return WindowKey.of_shot(get_config(), "2026-11-19T02:01:00.000Z", "weekly").text


def _ingest(client, key, start, day, n=6):
    body = _snapshot(key, start=start, shots=[_shot(i, ts=f"{day}T10:{i:02d}:00") for i in range(1, n + 1)])
    response = client.post(DESKTOP_URL, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _snapshot_body(athlete, sessions, granularity="weekly", window_key=None):
    return {"window_snapshot": {
        "athlete_id": athlete,
        "granularity": granularity,
        "window_key": window_key or _week_key(),
        "sessions": [{"session_id": sid, "revision": rev} for sid, rev in sessions],
    }}


def test_health_exposes_stable_db_instance_id(engine_env, tmp_path):
    client = _client(engine_env)
    first = client.get("/api/v1/health").json()
    assert first["db_instance_id"]
    from app.store.database import Database
    assert Database(engine_env[1]).db_instance_id == first["db_instance_id"]
    other = tmp_path / "other" / "facts.db"
    assert Database(str(other)).db_instance_id != first["db_instance_id"]


def test_snapshot_covers_verified_sessions_only(engine_env):
    client = _client(engine_env)
    a = _ingest(client, "session-a", "2026-11-19T10:00:00", DAY_A)
    b = _ingest(client, "session-b", "2026-11-20T10:00:00", DAY_B)
    athlete = a["athleteId"]
    endpoint = f"/api/v1/athletes/{athlete}/reports/weekly"
    week = _week_key()

    partial = client.post(endpoint, params={"week": week},
                          json=_snapshot_body(athlete, [(a["sessionId"], a["revision"])]))
    assert partial.status_code == 409, partial.text
    assert partial.json()["code"] == "snapshot_coverage"

    full = client.post(endpoint, params={"week": week},
                       json=_snapshot_body(athlete, [(a["sessionId"], a["revision"]),
                                                     (b["sessionId"], b["revision"])]))
    assert full.status_code == 200, full.text
    report = full.json()
    assert report["metrics"]["n_shots"] == 12
    assert report["window_snapshot"]["sessions"][0]["session_id"]
    assert report["window_snapshot"]["snapshot_id"]

    stale = client.post(endpoint, params={"week": week},
                        json=_snapshot_body(athlete, [(a["sessionId"], "0" * 64),
                                                      (b["sessionId"], b["revision"])]))
    assert stale.status_code == 409, stale.text
    assert stale.json()["code"] == "snapshot_mismatch"

    # 内容未变时命中同一快照缓存，不重复生成。
    a_cached = client.post(endpoint, params={"week": week},
                           json=_snapshot_body(athlete, [(a["sessionId"], a["revision"]),
                                                         (b["sessionId"], b["revision"])]))
    assert a_cached.status_code == 200
    assert a_cached.json()["cached"] is True


def test_snapshot_rejects_excluded_session(engine_env):
    client = _client(engine_env)
    a = _ingest(client, "session-a", "2026-11-19T10:00:00", DAY_A)
    b = _ingest(client, "session-b", "2026-11-20T10:00:00", DAY_B)
    athlete = a["athleteId"]
    excluded = client.post(f"/api/v1/athletes/{athlete}/sessions/{b['sessionId']}/exclude",
                           json={"actor_id": "coach-1", "reason": "测试排除"})
    assert excluded.status_code in (200, 204), excluded.text

    endpoint = f"/api/v1/athletes/{athlete}/reports/weekly"
    ok = client.post(endpoint, params={"week": _week_key()},
                     json=_snapshot_body(athlete, [(a["sessionId"], a["revision"])]))
    assert ok.status_code == 200, ok.text
    assert ok.json()["metrics"]["n_shots"] == 6

    bad = client.post(endpoint, params={"week": _week_key()},
                      json=_snapshot_body(athlete, [(a["sessionId"], a["revision"]),
                                                    (b["sessionId"], b["revision"])]))
    assert bad.status_code == 409, bad.text
    assert bad.json()["code"] == "snapshot_coverage"


def test_snapshot_rule_pack_version_mismatch(engine_env):
    client = _client(engine_env)
    a = _ingest(client, "session-a", "2026-11-19T10:00:00", DAY_A)
    athlete = a["athleteId"]
    body = _snapshot_body(athlete, [(a["sessionId"], a["revision"])])
    body["window_snapshot"]["rule_pack_version"] = "not-the-active-pack"

    response = client.post(f"/api/v1/athletes/{athlete}/reports/weekly",
                           params={"week": _week_key()}, json=body)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "snapshot_mismatch"


def test_snapshot_with_refresh_regenerates(engine_env):
    client = _client(engine_env)
    a = _ingest(client, "session-a", "2026-11-19T10:00:00", DAY_A)
    athlete = a["athleteId"]
    body = _snapshot_body(athlete, [(a["sessionId"], a["revision"])])
    endpoint = f"/api/v1/athletes/{athlete}/reports/weekly"

    first = client.post(endpoint, params={"week": _week_key()}, json=body)
    again = client.post(endpoint, params={"week": _week_key()}, json=body)
    refreshed = client.post(endpoint, params={"week": _week_key(), "refresh": "true"}, json=body)

    assert first.status_code == 200, first.text
    assert again.json()["cached"] is True
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["cached"] is False
    assert refreshed.json()["window_snapshot"]["snapshot_id"]


def test_unrecognized_report_body_is_rejected(engine_env):
    client = _client(engine_env)
    a = _ingest(client, "session-a", "2026-11-19T10:00:00", DAY_A)
    response = client.post(f"/api/v1/athletes/{a['athleteId']}/reports/weekly",
                           params={"week": _week_key()}, json={"unexpected": 1})
    assert response.status_code == 422, response.text


def _fail_second_arrow(client):
    db = _db()
    with db.lock, db.conn:
        db.conn.execute("""CREATE TRIGGER reject_new_shot BEFORE INSERT ON shot_fact
                           WHEN NEW.shot_seq=2 BEGIN SELECT RAISE(ABORT, 'write failed'); END""")


def _drop_trigger():
    db = _db()
    with db.lock, db.conn:
        db.conn.execute("DROP TRIGGER IF EXISTS reject_new_shot")


def _facts(session_id: str) -> int:
    return _db().query("SELECT COUNT(*) c FROM shot_fact WHERE session_id=?", (session_id,))[0]["c"]


def test_v12_rebuild_failure_keeps_old_facts_and_recovers(engine_env):
    import app.main as main
    client = TestClient(main.app, raise_server_exceptions=False)

    first = client.post(V12_URL, json=make_dt2(0)).json()
    assert first["results"][0]["status"] == "accepted"
    athlete = first["results"][0]["athlete_id"]
    session_id = f"{athlete}_{V12_LOCAL_DAY}_v01"
    assert _facts(session_id) == 1

    _fail_second_arrow(client)
    failed = client.post(V12_URL, json=make_dt2(1))
    assert failed.status_code == 500
    # 重建失败不得把旧事实删掉（同一事务回滚）。
    assert _facts(session_id) == 1
    assert _db().query("SELECT COUNT(*) c FROM v12_pending_rebuild")[0]["c"] == 1

    _drop_trigger()
    resent = client.post(V12_URL, json=make_dt2(1)).json()
    assert resent["results"][0]["status"] == "duplicate"
    assert _facts(session_id) == 2
    assert _db().query("SELECT COUNT(*) c FROM v12_pending_rebuild")[0]["c"] == 0

    # 同一箭只入一次。
    client.post(V12_URL, json=make_dt2(1))
    assert _facts(session_id) == 2


def test_v12_pending_rebuild_resumes_after_restart(engine_env):
    import app.main as main
    from app.ingest.v12.receiver import resume_pending_rebuilds
    client = TestClient(main.app, raise_server_exceptions=False)
    first = client.post(V12_URL, json=make_dt2(0)).json()
    athlete = first["results"][0]["athlete_id"]
    session_id = f"{athlete}_{V12_LOCAL_DAY}_v01"
    _fail_second_arrow(client)
    assert client.post(V12_URL, json=make_dt2(1)).status_code == 500
    assert _facts(session_id) == 1

    _drop_trigger()
    assert resume_pending_rebuilds(_db()) == 1
    assert _facts(session_id) == 2


def test_v12_rebuild_failure_keeps_period_report_then_updates(engine_env):
    import app.main as main
    client = TestClient(main.app, raise_server_exceptions=False)
    first = client.post(V12_URL, json=make_dt2(0)).json()
    athlete = first["results"][0]["athlete_id"]
    endpoint = f"/api/v1/athletes/{athlete}/reports/weekly"
    week = WindowKey.of_shot(__import__("app.config", fromlist=["get_config"]).get_config(),
                             "2026-09-07T01:00:00.000Z", "weekly").text

    before = client.post(endpoint, params={"week": week})
    assert before.status_code == 200, before.text
    assert before.json()["metrics"]["n_shots"] == 1

    _fail_second_arrow(client)
    assert client.post(V12_URL, json=make_dt2(1)).status_code == 500
    # 旧报告仍可读：失效发生在失败事务内，已回滚。
    still = client.post(endpoint, params={"week": week})
    assert still.status_code == 200, still.text
    assert still.json()["metrics"]["n_shots"] == 1

    _drop_trigger()
    client.post(V12_URL, json=make_dt2(1))
    after = client.post(endpoint, params={"week": week})
    assert after.status_code == 200, after.text
    assert after.json()["metrics"]["n_shots"] == 2
