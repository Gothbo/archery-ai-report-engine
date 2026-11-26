from fastapi.testclient import TestClient

from tests.conftest import seed_anchor, seed_session
from tests.test_desktop_ingest import URL, _client, _snapshot


def test_pre_ingest_exclusion_survives_late_upload_without_a_second_request(engine_env):
    client = _client(engine_env)
    first = client.post(URL, json=_snapshot("establish-identity")).json()
    athlete_id = first["athleteId"]
    base = f"/api/v1/athletes/{athlete_id}"
    alias = f"{base}/sessions/by-key/late-commit"

    missing = client.post(f"{alias}/exclude", json={})
    assert missing.status_code == 404
    reserved = client.post(f"{alias}/exclude", json={"reserve_if_missing": True})
    assert reserved.status_code == 200, reserved.text
    marker = reserved.json()
    assert marker["already_excluded"] is False
    assert client.post(f"{alias}/exclude", json={"reserve_if_missing": True}).json()["already_excluded"]
    assert client.get(f"/api/v1/sessions/{marker['session_id']}").status_code == 404

    # Ingest commits after the exclusion response. No follow-up exclusion is sent.
    late = client.post(URL, json=_snapshot("late-commit")).json()
    assert late["sessionId"] == marker["session_id"]
    assert client.get(f"/api/v1/sessions/{late['sessionId']}").json()["excluded"] is True
    report = client.post(f"{base}/reports/daily", json={
        "day": "2026-11-19", "session_ids": [first["sessionId"]],
    }).json()
    assert report["metrics"]["n_shots"] == 6
    stale = client.post(f"{base}/reports/daily", params={"session_id": late["sessionId"]})
    assert stale.status_code == 409
    assert client.post(f"{alias}/restore", json={}).status_code == 200
    assert client.get(f"/api/v1/sessions/{late['sessionId']}").json()["excluded"] is False


def test_reserved_exclusion_can_be_restored_before_first_upload(engine_env):
    client = _client(engine_env)
    athlete_id = client.post(URL, json=_snapshot("first")).json()["athleteId"]
    alias = f"/api/v1/athletes/{athlete_id}/sessions/by-key/restore-first"
    assert client.post(f"{alias}/exclude", json={"reserve_if_missing": True}).status_code == 200
    assert client.post(f"{alias}/restore", json={}).json()["already_active"] is False
    late = client.post(URL, json=_snapshot("restore-first")).json()
    assert client.get(f"/api/v1/sessions/{late['sessionId']}").json()["excluded"] is False


def test_excluded_anchor_stops_affecting_other_report_windows(engine_env):
    from app.main import app
    from app.store.database import get_database

    db = get_database()
    athlete = "athlete-a"
    seed_session(db, athlete, "anchor", "2026-11-10T01:00:00Z", [8] * 24)
    seed_session(db, athlete, "report-session", "2026-11-24T01:00:00Z", [10] * 24)
    anchor = seed_anchor(db, athlete, "anchor")
    client = TestClient(app)
    base = f"/api/v1/athletes/{athlete}"
    report_url = f"{base}/reports/weekly?week=2026-W48&view=coach"
    before = client.post(report_url).json()
    assert db.latest_snapshot(athlete, "anchor")["id"] == anchor
    assert before["conclusions"]["verdicts"]["avgScore"]["anchor"] == 8
    assert client.post(f"{base}/sessions/anchor/exclude", json={}).status_code == 200
    assert db.latest_snapshot(athlete, "anchor") is None
    assert client.get(f"/api/v1/reports/{before['report_id']}/content").status_code == 404
    assert not db.list_memories(athlete)
    excluded_report = client.post(report_url).json()
    assert excluded_report["conclusions"]["verdicts"]["avgScore"].get("anchor") is None
    assert client.post(f"{base}/sessions/anchor/restore", json={}).status_code == 200
    assert db.latest_snapshot(athlete, "anchor")["id"] == anchor
    assert client.get(f"/api/v1/reports/{excluded_report['report_id']}/content").status_code == 404
    assert client.post(report_url).json()["conclusions"]["verdicts"]["avgScore"]["anchor"] == 8


def test_exclude_restore_idempotency_and_read_fields(engine_env):
    from app.main import app
    from app.store.database import get_database

    db = get_database()
    seed_session(db, "athlete-a", "s-a", "2026-11-24T01:00:00Z", [9, 8, 10])
    client = TestClient(app)
    url = "/api/v1/athletes/athlete-a/sessions/s-a"
    excluded = client.post(url + "/exclude", json={"actor_id": "coach", "reason": "athlete-deleted"})
    assert excluded.status_code == 200
    assert excluded.json()["already_excluded"] is False
    audit_count = db.query("SELECT COUNT(*) n FROM session_lifecycle_audit")[0]["n"]
    assert client.post(url + "/exclude", json={}).json()["already_excluded"] is True
    assert db.query("SELECT COUNT(*) n FROM session_lifecycle_audit")[0]["n"] == audit_count
    assert client.get("/api/v1/athletes/athlete-a/sessions").json()["sessions"] == []
    listed = client.get("/api/v1/athletes/athlete-a/sessions?include_excluded=true").json()["sessions"]
    assert listed[0]["excluded"] is True
    detail = client.get("/api/v1/sessions/s-a").json()
    assert detail["excluded"] is True and detail["shots"] == []
    report = client.post("/api/v1/athletes/athlete-a/reports/daily", params={"session_id": "s-a"})
    assert report.status_code == 409
    assert report.json()["code"] == "session_excluded"
    restored = client.post(url + "/restore", json={"actor_id": "admin"})
    assert restored.status_code == 200 and restored.json()["already_active"] is False
    assert len(client.get("/api/v1/athletes/athlete-a/sessions").json()["sessions"]) == 1
    restored_report = client.post("/api/v1/athletes/athlete-a/reports/daily", params={"session_id": "s-a"})
    assert restored_report.status_code == 200
    assert restored_report.json()["metrics"]["n_shots"] == 3
    assert client.post(url + "/restore", json={}).json()["already_active"] is True


def test_exclusion_hides_cross_athlete_and_unknown_and_supports_by_key(engine_env):
    from app.main import app
    from app.ingest.desktop import session_id_for
    from app.store.database import get_database

    seed_session(get_database(), "athlete-a", "d_athlete-a_key-one", "2026-11-24T01:00:00Z", [9, 8])
    client = TestClient(app)
    base = "/api/v1/athletes/athlete-b/sessions/"
    assert client.post(base + "d_athlete-a_key-one/exclude", json={}).status_code == 404
    assert client.post(base + "missing/exclude", json={}).status_code == 404
    session_id = session_id_for("athlete-a", "key-one")
    # The alias exercises the same deterministic ID transform as desktop ingest.
    assert session_id == "d_athlete-a_key-one"
    response = client.post("/api/v1/athletes/athlete-a/sessions/by-key/key-one/exclude", json={})
    assert response.status_code == 200 and response.json()["session_id"] == session_id


def test_reingest_same_key_does_not_restore_excluded_session(engine_env):
    from app.main import app
    from tests.test_desktop_ingest import _snapshot, URL

    client = TestClient(app)
    imported = client.post(URL, json=_snapshot()).json()
    athlete_id, session_id = imported["athleteId"], imported["sessionId"]
    assert client.post(f"/api/v1/athletes/{athlete_id}/sessions/{session_id}/exclude", json={}).status_code == 200
    again = client.post(URL, json=_snapshot()).json()
    assert again["sessionId"] == session_id
    detail = client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["excluded"] is True and detail["shots"] == []


def test_calendar_report_and_ask_are_invalidated_on_exclude_and_restore(engine_env):
    from tests.test_desktop_ingest import URL, _client, _shot, _snapshot

    client = _client(engine_env)
    # 首箭是上海时间前一天的试射，日报按首支计分箭归日。
    first = client.post(URL, json=_snapshot("late-ack", shots=[
        _shot(1, ts="2026-11-19T15:59:00Z", trial=True),
        _shot(2, ts="2026-11-19T16:01:00Z", score=10),
    ])).json()
    second = client.post(URL, json=_snapshot("other-session", shots=[
        _shot(1, ts="2026-11-20T09:00:00", score=8),
    ])).json()
    unrelated = client.post(URL, json=_snapshot()).json()
    athlete = f"/api/v1/athletes/{first['athleteId']}"
    ids = [first["sessionId"], second["sessionId"]]

    def generate(day, session_ids):
        response = client.post(f"{athlete}/reports/daily", json={"day": day, "session_ids": session_ids})
        assert response.status_code == 200, response.text
        return response.json()

    def assert_unavailable(report):
        report_id = report["report_id"]
        assert client.get(f"/api/v1/reports/{report_id}/content?view=coach").status_code == 404
        listing = client.get(f"{athlete}/reports").json()["reports"]
        assert report_id not in {row["report_id"] for row in listing}
        assert client.post(f"{athlete}/ask", json={
            "question": "本期均环是多少？", "granularity": "daily",
            "window_key": report["window_key"], "report_id": report_id,
        }).status_code == 400

    unaffected = generate("2026-11-19", [unrelated["sessionId"]])
    original = generate("2026-11-20", ids)
    assert original["session_ids"] == sorted(ids)
    assert original["metrics"]["avg_score"] == 9
    alias = f"{athlete}/sessions/by-key/late-ack"
    assert client.post(f"{alias}/exclude", json={}).status_code == 200
    assert_unavailable(original)
    assert client.get(f"/api/v1/reports/{unaffected['report_id']}/content").status_code == 200
    stale = client.post(f"{athlete}/reports/daily", json={"day": "2026-11-20", "session_ids": ids})
    assert stale.status_code == 409 and stale.json()["code"] == "session_excluded"

    reduced = generate("2026-11-20", [second["sessionId"]])
    assert reduced["metrics"]["avg_score"] == 8
    restored = client.post(f"{alias}/restore", json={})
    assert restored.status_code == 200, restored.text
    assert restored.json()["session_id"] == first["sessionId"]
    assert restored.json()["already_active"] is False
    assert_unavailable(reduced)
    regenerated = generate("2026-11-20", ids)
    repeated = client.post(f"{alias}/restore", json={})
    assert repeated.json()["already_active"] is True
    assert repeated.json()["restored_at_utc"] == restored.json()["restored_at_utc"]
    assert client.get(f"/api/v1/reports/{regenerated['report_id']}/content").status_code == 200
    assert regenerated["metrics"]["avg_score"] == 9
    unknown = client.post(f"{athlete}/sessions/by-key/unknown/restore", json={})
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "session_not_found"


def test_upgrade_removes_pre_transition_calendar_cache(engine_env):
    from app.store.database import Database, get_database
    from tests.test_desktop_ingest import URL, _client, _snapshot

    client = _client(engine_env)
    imported = client.post(URL, json=_snapshot()).json()
    athlete_id, session_id = imported["athleteId"], imported["sessionId"]
    assert client.post(f"/api/v1/athletes/{athlete_id}/sessions/{session_id}/exclude", json={}).status_code == 200
    db = get_database()
    db.put_cached_report("old-calendar", athlete_id, "daily", "daily:day:2026-11-19", None, "{}")
    with db.lock, db.conn:
        db.conn.execute("UPDATE report_cache SET generated_at_utc='2000-01-01T00:00:00.000Z'")

    reopened = Database(db.db_path)
    try:
        assert reopened.get_report_row("old-calendar") is None
    finally:
        reopened.close()


def test_exclusion_invalidates_window_cache_and_weekly_report_omits_session(engine_env):
    from app.main import app
    from app.store.database import get_database

    db = get_database()
    scores = [9, 8, 10, 9, 8, 10, 9, 8, 10, 9, 8, 10, 9, 8, 10, 9, 8, 10, 9, 8, 10, 9]
    seed_session(db, "athlete-a", "window-a", "2026-11-24T01:00:00Z", scores)
    client = TestClient(app)
    window = "2026-W48"
    db.put_cached_report("weekly-cache", "athlete-a", "weekly", window, None, "{}")
    response = client.post("/api/v1/athletes/athlete-a/reports/weekly", params={"week": window})
    assert response.status_code == 200
    assert response.json()["metrics"]["n_shots"] == len(scores)
    assert client.post("/api/v1/athletes/athlete-a/sessions/window-a/exclude", json={}).status_code == 200
    assert db.get_report_row("weekly-cache") is None
    response = client.post("/api/v1/athletes/athlete-a/reports/weekly", params={"week": window, "refresh": "true"})
    assert response.status_code == 200
    assert response.json()["metrics"]["n_shots"] == 0


def test_exclude_and_restore_invalidate_windows_containing_later_arrows(engine_env):
    from tests.test_desktop_ingest import URL, _assert_report_invalidated, _client, _shot, _snapshot

    client = _client(engine_env)
    # Local midnight crosses the ISO week, calendar month, quarter and year.
    result = client.post(URL, json=_snapshot("overnight", shots=[
        _shot(1, ts="2023-12-31T23:55:00+08:00"),
        _shot(2, ts="2024-01-01T00:05:00+08:00"),
    ])).json()
    athlete = f"/api/v1/athletes/{result['athleteId']}"
    session = f"{athlete}/sessions/{result['sessionId']}"
    windows = [
        ("weekly", {"week": "2024-W01"}),
        ("weekly", {"week": "2024-1"}),
        ("monthly", {"month": "2024-01"}),
        ("quarterly", {"quarter": "2024Q1"}),
        ("yearly", {"year": "2024"}),
    ]

    def reports(shots):
        bodies = []
        for granularity, params in windows:
            response = client.post(f"{athlete}/reports/{granularity}", params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["metrics"]["n_shots"] == shots
            bodies.append(body)
        return bodies

    original = reports(1)
    assert client.post(f"{session}/exclude", json={}).status_code == 200
    for report in original:
        _assert_report_invalidated(client, athlete, report)

    reduced = reports(0)
    assert client.post(f"{session}/restore", json={}).status_code == 200
    for report in reduced:
        _assert_report_invalidated(client, athlete, report)
    reports(1)


def test_exclude_and_restore_invalidate_later_reports_using_the_session_as_comparison(engine_env):
    from tests.test_desktop_ingest import URL, _assert_report_invalidated, _client, _shot, _snapshot

    client = _client(engine_env)
    previous = client.post(URL, json=_snapshot("previous")).json()
    current = client.post(URL, json=_snapshot("current", shots=[
        _shot(1, ts="2026-11-26T10:00:00"),
    ])).json()
    athlete = f"/api/v1/athletes/{current['athleteId']}"
    session = f"{athlete}/sessions/{previous['sessionId']}"
    windows = [
        ("daily", {"session_id": current["sessionId"]}),
        ("weekly", {"week": "2026-W48"}),
    ]

    def reports(compared):
        bodies = []
        for granularity, params in windows:
            response = client.post(f"{athlete}/reports/{granularity}", params={**params, "view": "coach"})
            assert response.status_code == 200, response.text
            body = response.json()
            assert (body["conclusions"]["self_compare"]["previous"] is not None) is compared
            assert ("rolling_baseline" in body["coach_extra"]) is compared
            bodies.append(body)
        return bodies

    original = reports(True)
    assert client.post(f"{session}/exclude", json={}).status_code == 200
    for report in original:
        _assert_report_invalidated(client, athlete, report)

    reduced = reports(False)
    assert client.post(f"{session}/restore", json={}).status_code == 200
    for report in reduced:
        _assert_report_invalidated(client, athlete, report)
    reports(True)


def test_ask_has_no_excluded_report_source(engine_env):
    from app.main import app
    from app.store.database import get_database

    db = get_database()
    seed_session(db, "athlete-a", "ask-a", "2026-11-24T01:00:00Z", [10, 9, 8])
    client = TestClient(app)
    client.post("/api/v1/athletes/athlete-a/reports/daily", params={"session_id": "ask-a"})
    client.post("/api/v1/athletes/athlete-a/sessions/ask-a/exclude", json={})
    answer = client.post("/api/v1/athletes/athlete-a/ask", json={"question": "训练表现如何"})
    assert answer.status_code in (200, 400)
    if answer.status_code == 200:
        assert all(source.get("ref") != "ask-a" for source in answer.json()["sources"])

