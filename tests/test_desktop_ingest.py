# -*- coding: utf-8 -*-
"""桌面端场次快照接口：camelCase 契约 / 场次边界 / 幂等 / 缺测 / 出日报。"""
from __future__ import annotations

from fastapi.testclient import TestClient

URL = "/api/v1/ingest/desktop"


def _shot(seq: int, *, ts: str, score: int = 9, miss: bool = False, trial: bool = False,
          hr: float | None = 90, wind: float | None = 1.2, mcr: float | None = 0.42,
          x: float | None = 30.0, y: float | None = -20.0) -> dict:
    return {
        "seq": seq, "xMm": x, "yMm": y, "score": score,
        "isX": False, "isMiss": miss, "isTrial": trial, "ts": ts,
        "heartRate": hr, "windSpeed": wind, "windDirDeg": 90.0, "mcrT": mcr,
    }


def _snapshot(session_key: str = "session-a", *, start: str = "2026-11-19T10:00:00",
              shots: list[dict] | None = None, athlete: str = "person-guid-a") -> dict:
    return {
        "athlete": {"key": athlete, "name": "张三", "bowType": "recurve"},
        "session": {"key": session_key, "startedAt": start,
                    "endedAt": "2026-11-19T11:00:00", "distanceM": 70, "subject": "记分"},
        "shots": shots if shots is not None else [
            _shot(i, ts=f"2026-11-19T10:{i:02d}:00") for i in range(1, 7)
        ],
    }


def _client(engine_env) -> TestClient:
    import app.main as main
    return TestClient(main.app)


def _db():
    from app.store.database import get_database
    return get_database()


def test_ingest_desktop_creates_session_and_report(engine_env):
    client = _client(engine_env)
    r = client.post(URL, json=_snapshot())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["shotCount"] == 6
    assert body["bowType"] == "反曲弓"
    assert body["sessionId"].startswith("d_")
    assert body["athleteId"].isdigit()
    # 无时区时间按引擎时区（Asia/Shanghai）解释 → UTC 02:01（首箭时间）
    assert body["sessionTimeUtc"] == "2026-11-19T02:01:00.000Z"

    detail = client.get(f"/api/v1/sessions/{body['sessionId']}")
    assert detail.status_code == 200, detail.text
    shots = detail.json()["shots"]
    assert len(shots) == 6
    assert shots[0]["x_mm"] == 30.0
    assert shots[0]["hr"] == 90
    assert shots[0]["mcr_t"] == 0.42

    report = client.post(
        f"/api/v1/athletes/{body['athleteId']}/reports/daily",
        params={"session_id": body["sessionId"]})
    assert report.status_code == 200, report.text
    assert report.json()["sections"]

    profile = client.get(f"/api/v1/athletes/{body['athleteId']}").json()["profile"]
    assert profile["name"] == "张三"
    assert profile["bow_type"] == "反曲弓"


def test_reupload_replaces_session_and_keeps_boundary(engine_env):
    client = _client(engine_env)
    first = client.post(URL, json=_snapshot()).json()

    # 同一天、开赛时间只差 1 分钟的另一场：必须保持两条场次，不能按间隔合并
    second = client.post(URL, json=_snapshot(
        "session-b", start="2026-11-19T10:01:00",
        shots=[_shot(1, ts="2026-11-19T10:01:00", score=10)])).json()
    assert second["sessionId"] != first["sessionId"]

    sessions = client.get(f"/api/v1/athletes/{first['athleteId']}/sessions").json()["sessions"]
    assert len(sessions) == 2

    # 重传同一场（改环值）→ 仍是 6 箭，不重复累加
    changed = _snapshot()
    for shot in changed["shots"]:
        shot["score"] = 10
    again = client.post(URL, json=changed).json()
    assert again["sessionId"] == first["sessionId"]
    shots = client.get(f"/api/v1/sessions/{first['sessionId']}").json()["shots"]
    assert len(shots) == 6
    assert all(s["score"] == 10.0 for s in shots)


def test_miss_has_no_coordinates_and_zero_score(engine_env):
    client = _client(engine_env)
    body = client.post(URL, json=_snapshot(shots=[
        _shot(1, ts="2026-11-19T10:00:00", score=0, miss=True, x=0, y=0),
        _shot(2, ts="2026-11-19T10:00:30", score=10),
    ])).json()
    shots = client.get(f"/api/v1/sessions/{body['sessionId']}").json()["shots"]
    assert shots[0]["score"] == 0.0
    assert shots[0]["hit"] == 0
    assert shots[0]["x_mm"] is None and shots[0]["y_mm"] is None
    assert shots[1]["score"] == 10.0


def test_warnings_when_sensors_missing(engine_env):
    client = _client(engine_env)
    body = client.post(URL, json=_snapshot(shots=[
        _shot(1, ts="2026-11-19T10:00:00", hr=0, wind=-1, mcr=0),
        _shot(2, ts="2026-11-19T10:00:30", hr=0, wind=-1, mcr=0),
    ])).json()
    text = "；".join(body["warnings"])
    assert "心率" in text
    assert "风" in text
    assert "撒放用时" in text


def test_empty_shots_rejected(engine_env):
    client = _client(engine_env)
    r = client.post(URL, json=_snapshot(shots=[]))
    assert r.status_code == 400


def test_trial_shots_not_in_scoring_mode(engine_env):
    client = _client(engine_env)
    body = client.post(URL, json=_snapshot(shots=[
        _shot(1, ts="2026-11-19T10:00:00", trial=True, score=8),
        _shot(2, ts="2026-11-19T10:00:30", score=9),
    ])).json()
    shots = client.get(f"/api/v1/sessions/{body['sessionId']}").json()["shots"]
    assert shots[0]["shooting_mode"] == 0
    assert shots[1]["shooting_mode"] == 1
