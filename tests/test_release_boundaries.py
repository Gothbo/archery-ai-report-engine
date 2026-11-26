"""Cross-feature release checks for the desktop contract, snapshots and model input."""
import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest

from app.api import ask, guidance
from app.llm.client import MockLLMClient
from app.memory.notes import add_note, close_note
from app.reports import generator
from app.store.database import get_database
from tests.test_guidance_interpret import _json, report_case
from tests.test_guidance_stream import parse_sse


def test_engine_advertises_the_desktop_contract(report_case):
    assert _json(report_case[0].get("/api/v1/health"))["desktop_report_contract"] == 2


@pytest.mark.parametrize(("endpoint", "module"), [("ask", ask), ("guidance/stream", guidance)])
def test_legacy_model_routes_use_the_same_filtered_input(report_case, monkeypatch, endpoint, module):
    client, base, report, request, _ = report_case
    athlete_id = report["athlete"]["id"]
    add_note(get_database(), athlete_id, "goal", "FreeTextFixture 13500138000 Details.mp4", "athlete", "fixture")
    mock = MockLLMClient(responses=["按报告安排练习。"])
    monkeypatch.setattr(module, "get_llm_client", lambda _: mock)
    query = {**request, "view": "athlete"}
    if endpoint == "ask":
        query["question"] = "示例运动员甲想核对报告，请勿拨打 13500138000。"
    response = client.post(f"{base}/{endpoint}", json=query)
    assert response.status_code == 200, response.text
    message = mock.calls[0][0]["content"]
    payload = json.loads(message.split("skeleton_json: ", 1)[1])
    assert "athlete" not in payload
    assert not any(section.get("evidence") for section in payload["sections"])
    assert "FreeTextFixture" not in message
    assert athlete_id not in message and "示例运动员甲" not in message
    assert all("13500138000" not in m["content"] for m in mock.calls[0])


@pytest.mark.parametrize("endpoint", ["ask", "guidance/stream"])
def test_coach_read_cannot_create_a_missing_report(report_case, endpoint):
    client, base, _, _, _ = report_case
    before = _json(client.get(f"{base}/reports"))
    request = {"granularity": "weekly", "window_key": "2026-W01", "view": "coach"}
    if endpoint == "ask":
        request["question"] = "如何看报告？"
    assert client.post(f"{base}/{endpoint}", json=request).status_code == 404
    assert _json(client.get(f"{base}/reports")) == before


def test_stream_drops_final_sources_when_a_note_closes_during_generation(report_case, monkeypatch):
    client, base, report, request, _ = report_case
    db = get_database()
    athlete_id = report["athlete"]["id"]
    note = add_note(db, athlete_id, "coach_note", "只限已授权视角的备注", "coach", "fixture")

    def respond(_messages):
        assert close_note(db, athlete_id, note)
        return "本期有训练记录。"

    monkeypatch.setattr(guidance, "get_llm_client", lambda _: MockLLMClient(responder=respond))
    response = client.post(f"{base}/guidance/stream", json={**request, "view": "coach"})
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "COACH-CONTEXT-STALE"
    assert "只限已授权视角的备注" not in response.text


def test_failed_refresh_rolls_back_report_and_observation(report_case, monkeypatch):
    client, base, report, _, _ = report_case
    original = dict(get_database().get_report_row(report["report_id"]))
    original_persist = generator.persist_report

    def fail_after_persistence(db, draft):
        original_persist(db, draft)
        raise ValueError("synthetic failure after writing the new snapshot")

    monkeypatch.setattr(generator, "persist_report", fail_after_persistence)
    response = client.post(f"{base}/reports/daily", json={
        "day": "2026-11-19", "session_ids": report["session_ids"],
    })
    assert response.status_code == 400
    db = get_database()
    assert dict(db.get_report_row(report["report_id"])) == original
    assert [row["report_id"] for row in db.query("SELECT report_id FROM guidance_report_observations")] == [
        report["report_id"],
    ]


def test_exclude_serializes_with_period_report_publication(report_case, monkeypatch):
    client, base, report, _, _ = report_case
    db = get_database()
    athlete_id = report["athlete"]["id"]
    session_id = report["session_ids"][0]
    draft_ready, resume, exclude_started = Event(), Event(), Event()
    original_build = generator.build_report

    def hold_draft(*args, **kwargs):
        draft = original_build(*args, **kwargs)
        draft_ready.set()
        assert resume.wait(timeout=8)
        return draft

    def exclude():
        exclude_started.set()
        return db.set_session_excluded(athlete_id, session_id, "fixture", "audit")

    monkeypatch.setattr(generator, "build_report", hold_draft)
    with ThreadPoolExecutor(max_workers=2) as tasks:
        pending = tasks.submit(client.post, f"{base}/reports/weekly", params={"week": "2026-W47"})
        assert draft_ready.wait(timeout=5)
        removal = tasks.submit(exclude)
        try:
            assert exclude_started.wait(timeout=5)
            try:
                removal.result(timeout=0.3)
            except TimeoutError:
                pass
        finally:
            resume.set()
        published = _json(pending.result(timeout=5))
        removal.result(timeout=5)
    assert client.get(f"/api/v1/reports/{published['report_id']}/content").status_code == 404
