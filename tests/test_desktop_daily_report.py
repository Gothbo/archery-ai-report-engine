from app.config import get_config
from tests.test_desktop_ingest import URL, _client, _shot, _snapshot


def test_daily_report_uses_confirmed_desktop_sessions_and_is_idempotent(engine_env):
    client = _client(engine_env)
    first = client.post(URL, json=_snapshot()).json()
    second_snapshot = _snapshot(
        "session-b", start="2026-11-19T15:00:00",
        shots=[_shot(1, ts="2026-11-19T15:01:00", score=10)],
    )
    second = client.post(URL, json=second_snapshot).json()
    assert client.post(URL, json=second_snapshot).json()["sessionId"] == second["sessionId"]
    other = client.post(URL, json=_snapshot(
        "session-c", start="2026-11-20T09:00:00",
        shots=[_shot(1, ts="2026-11-20T09:01:00", score=10)],
    )).json()
    endpoint = f"/api/v1/athletes/{first['athleteId']}/reports/daily"

    response = client.post(endpoint, json={
        "day": "2026-11-19", "session_ids": [first["sessionId"], second["sessionId"]],
    })
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["window_key"] == "daily:day:2026-11-19"
    assert report["metrics"]["n_shots"] == 7
    assert report["metrics"]["avg_score"] == 9.14
    assert next(section for section in report["sections"] if section["key"] == "window_score")["title"] == "当日成绩"

    wrong_day = client.post(endpoint, json={
        "day": "2026-11-19", "session_ids": [other["sessionId"]],
    })
    assert wrong_day.status_code == 404

    unrelated = client.post(URL, json=_snapshot("other-athlete-session", athlete="other-athlete")).json()
    wrong_athlete = client.post(endpoint, json={
        "day": "2026-11-19", "session_ids": [unrelated["sessionId"]],
    })
    assert wrong_athlete.status_code == 404

    ask = client.post(f"/api/v1/athletes/{first['athleteId']}/ask", json={
        "question": "本期均环是多少？", "granularity": "daily",
        "window_key": report["window_key"], "report_id": report["report_id"],
    })
    assert ask.status_code == 200, ask.text
    assert ask.json()["window_key"] == report["window_key"]

    ask_legacy = client.post(f"/api/v1/athletes/{first['athleteId']}/ask", json={
        "question": "本期均环是多少？", "granularity": "daily", "window_key": report["window_key"],
    })
    assert ask_legacy.status_code == 200, ask_legacy.text

    ask_stale = client.post(f"/api/v1/athletes/{first['athleteId']}/ask", json={
        "question": "本期均环是多少？", "granularity": "daily",
        "window_key": report["window_key"], "report_id": "unknown-report",
    })
    assert ask_stale.status_code == 400


def test_mock_answers_calendar_daily_mean_without_inventing_progress(engine_env, monkeypatch):
    llm = get_config().llm
    monkeypatch.setattr(llm, "enabled", True)
    monkeypatch.setattr(llm, "provider", "mock")
    client = _client(engine_env)
    first = client.post(URL, json=_snapshot(shots=[
        _shot(1, ts="2026-11-19T10:01:00", score=9),
    ])).json()
    second = client.post(URL, json=_snapshot("session-b", shots=[
        _shot(1, ts="2026-11-19T10:02:00", score=0, miss=True, x=None, y=None),
    ])).json()
    athlete = f"/api/v1/athletes/{first['athleteId']}"
    report_response = client.post(f"{athlete}/reports/daily", json={
        "day": "2026-11-19", "session_ids": [first["sessionId"], second["sessionId"]],
    })
    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    assert report["metrics"]["avg_score"] == 4.5
    assert report["metrics"]["effective_avg_score"] == 9

    context = {"granularity": "daily", "window_key": report["window_key"], "report_id": report["report_id"]}
    response = client.post(f"{athlete}/ask", json={**context, "question": "本期均环是多少？"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result["degraded"], result
    assert "当日平均环 4.50" in result["answer"]
    assert "进步" not in result["answer"]
    assert any(source["title"] == "当日成绩" and "含脱靶均环 4.50" in source["summary"]
               for source in result["sources"])

    effective = client.post(f"{athlete}/ask", json={**context, "question": "不含脱靶的有效箭均环是多少？"})
    assert effective.status_code == 200, effective.text
    assert effective.json()["answer"].startswith("当日有效箭均环 9.00")

    overview = client.post(f"{athlete}/ask", json={**context, "question": "今天训练怎么样？"})
    assert overview.status_code == 200, overview.text
    assert not overview.json()["degraded"]
    assert "进步" not in overview.json()["answer"]
