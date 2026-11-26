"""Read-only model expression over a stored, role-filtered report."""
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.api.guidance import GUIDANCE_HEADINGS
from app.config import get_config
from app.llm.client import LLMError, MockLLMClient
from app.llm.mock import demo_responder
from app.llm.tasks import get_registry
from app.main import app
from app.memory.notes import add_note, close_note
from app.rag.context import extract_numbers
from app.store.database import get_database
from tests.conftest import _make_temp_config
from tests.test_desktop_ingest import URL as INGEST, _snapshot
from tests.test_guidance_stream import use_mock


@pytest.fixture()
def report_case(engine_env, tmp_path):
    def configure(**llm):
        cfg = _make_temp_config(tmp_path, mdc_source="v1-trial-20261010")
        cfg["llm"] = {"enabled": True, "provider": "mock", "model_version": "expression-test", **llm}
        (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        get_config.cache_clear()

    configure()
    client = TestClient(app)
    payload = _snapshot()
    payload["athlete"]["name"] = "示例运动员甲"
    receipt = _json(client.post(INGEST, json=payload))
    base = f"/api/v1/athletes/{receipt['athleteId']}"
    report = _json(client.post(f"{base}/reports/daily",
                               json={"day": "2026-11-19", "session_ids": [receipt["sessionId"]]}))
    body = {"granularity": "daily", "window_key": report["window_key"], "report_id": report["report_id"]}
    return client, base, report, body, configure


def _json(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _interpret(case, **overrides):
    client, base, _, body, _ = case
    return client.post(f"{base}/guidance/interpret", json={**body, **overrides})


def test_model_expression_is_read_only_and_references_its_input(report_case, monkeypatch):
    client, base, report, _, _ = report_case
    mock = use_mock(monkeypatch, MockLLMClient(responder=demo_responder))
    stored = dict(get_database().get_report_row(report["report_id"]))
    assert report["rule_guidance"]["source"] == "rule"
    assert report["rule_guidance"]["degraded_reason"] is None

    result = _json(_interpret(report_case))
    repeated = _json(_interpret(report_case, report_id=None))
    assert result["source"] == "model" and result["degraded_reason"] is None
    assert result["rule_pack"] == report["rule_pack"]
    assert result["model_version"] == "expression-test"
    assert result["generated_at_utc"].endswith("Z")
    assert [s["heading"] for s in result["sections"]] == list(GUIDANCE_HEADINGS)
    assert all(s["text"] for s in result["sections"])
    assert result["context_version"] == repeated["context_version"]
    assert result["references"] == repeated["references"]
    refs = {r["ref"] for r in result["references"]}
    assert {"metric:avgScore", f"window:daily:{report['window_key']}"} <= refs
    assert {f"rule:{b['source']}" for b in report["rule_guidance"]["bullets"]} <= refs
    payload = json.loads(mock.calls[0][0]["content"].split("skeleton_json: ", 1)[1])
    assert set(extract_numbers(result["text"])) <= set(extract_numbers(json.dumps(payload, ensure_ascii=False)))
    assert dict(get_database().get_report_row(report["report_id"])) == stored
    assert len(_json(client.get(f"{base}/reports"))["reports"]) == 1
    after = _json(client.get(f"/api/v1/reports/{report['report_id']}/content"))
    assert all(after[key] == report[key] for key in ("metrics", "conclusions", "rule_pack"))


def test_disabled_returns_the_saved_rule_guidance(report_case, monkeypatch):
    _, _, report, _, configure = report_case
    configure(enabled=False)
    mock = use_mock(monkeypatch, MockLLMClient(error=AssertionError("model must stay disabled")))
    result = _json(_interpret(report_case))
    _assert_fallback(result, "disabled", report)
    assert mock.calls == []


def _assert_fallback(result, reason, report):
    guide = report["rule_guidance"]
    assert result["source"] == "rule" and result["degraded_reason"] == reason
    assert result["model_version"] is None and result["sections"] == []
    assert result["rule_guidance"] == guide
    assert result["text"] == "\n".join([guide["summary"], *(b["text"] for b in guide["bullets"])])
    assert result["text"].strip()


@pytest.mark.parametrize(("reply", "reason"), [
    ("治疗方案：服用止痛药。", "guardrail_failed"),
    ("技术明显退步，必须立刻加练。", "guardrail_failed"),
    ("本期可提高三成。", "quality_failed"),
    ("本期平均环为 987.321 环。", "quality_failed"),
])
def test_unapproved_claims_are_replaced_by_rule_guidance(report_case, monkeypatch, reply, reason):
    use_mock(monkeypatch, MockLLMClient(responses=[f"五、提升方案\n{reply}"]))
    result = _json(_interpret(report_case))
    _assert_fallback(result, reason, report_case[2])
    assert reply not in result["text"]


@pytest.mark.parametrize(("options", "reason"), [
    ({"responses": ["   "]}, "empty"),
    ({"error": LLMError("upstream unavailable")}, "error"),
    ({"error": TimeoutError("connect timed out")}, "timeout"),
])
def test_upstream_failure_modes(report_case, monkeypatch, options, reason):
    mock = use_mock(monkeypatch, MockLLMClient(**options))
    _assert_fallback(_json(_interpret(report_case)), reason, report_case[2])
    assert mock.stream_closed and get_registry().current() is None


@pytest.mark.parametrize(("config", "model"), [
    ({"first_token_timeout_sec": 0.02}, {"first_token_delay": 0.5}),
    ({"idle_timeout_sec": 0.02}, {"hang_after": 1}),
    ({"first_token_timeout_sec": 0.5, "total_timeout_sec": 0.02}, {"first_token_delay": 0.5}),
])
def test_deadlines_close_upstream_and_release_registry(report_case, monkeypatch, config, model):
    report_case[4](**config)
    mock = use_mock(monkeypatch, MockLLMClient(responses=["可以按报告复核。"], **model))
    _assert_fallback(_json(_interpret(report_case)), "timeout", report_case[2])
    assert mock.stream_closed and get_registry().current() is None


def test_busy_registry_uses_rule_fallback(report_case, monkeypatch):
    mock = use_mock(monkeypatch, MockLLMClient(error=AssertionError("busy request reached model")))
    registry = get_registry()
    active = registry.try_acquire(kind="ask", view="coach", deadline_sec=10,
                                  tz=get_config().timezone, cancellable=False)
    try:
        _assert_fallback(_json(_interpret(report_case)), "busy", report_case[2])
        assert registry.current() is active and mock.calls == []
    finally:
        registry.release(active)


def test_invalid_requests_and_missing_reports_never_generate(report_case):
    client, base, report, body, _ = report_case
    assert _interpret(report_case, granularity="hourly").status_code == 400
    assert _interpret(report_case, granularity="weekly", window_key="2026-W00").status_code == 400
    assert _interpret(report_case, window_key="daily:day:2026-02-31").status_code == 400
    assert _interpret(report_case, view="admin").status_code == 400
    assert _interpret(report_case, report_id="missing").status_code == 404
    assert _interpret(report_case, report_id=None, window_key="daily:day:2026-11-20").status_code == 404
    assert _interpret(report_case, window_key="daily:day:2026-11-20").status_code == 404
    assert client.post("/api/v1/athletes/missing/guidance/interpret", json=body).status_code == 404
    assert [row["report_id"] for row in _json(client.get(f"{base}/reports"))["reports"]] == [report["report_id"]]


def test_model_prompt_keeps_visible_note_references_without_raw_identifiers(report_case, monkeypatch):
    _, _, report, _, _ = report_case
    athlete_id = report["athlete"]["id"]
    db = get_database()
    visible = add_note(db, athlete_id, "goal", "王示例的目标 13500138000、11010519491231002X，见动作.mp4",
                       "athlete", "actor-secret")
    hidden = add_note(db, athlete_id, "coach_note", "内部专属细节", "coach", "coach-secret")
    mock = use_mock(monkeypatch, MockLLMClient(responder=demo_responder))
    result = _json(_interpret(report_case))
    assert result["source"] == "model"
    prompt = mock.calls[0][0]["content"]
    payload = json.loads(prompt.split("skeleton_json: ", 1)[1])
    for secret in ("示例运动员甲", "王示例", athlete_id, "13500138000", "11010519491231002X", "动作.mp4",
                   "actor-secret", "coach-secret", "内部专属细节"):
        assert secret not in prompt
    assert payload["rule_pack_version"] == report["rule_pack"]["version"]
    assert all(not section.get("evidence") for section in payload["sections"])
    refs = {row["ref"] for row in result["references"]}
    assert f"note:{visible}" in refs and f"note:{hidden}" not in refs
    assert f"note:{visible}" in prompt and f"note:{hidden}" not in prompt
    coach = _json(_interpret(report_case, view="coach"))
    assert f"note:{hidden}" in {row["ref"] for row in coach["references"]}


def test_legacy_rule_guidance_metadata_is_added_on_read_without_rewriting(report_case):
    client, _, report, _, _ = report_case
    db = get_database()
    stored = deepcopy(json.loads(db.get_report_row(report["report_id"])["report_json"]))
    stored["rule_guidance"].pop("source", None)
    stored["rule_guidance"].pop("degraded_reason", None)
    with db.lock, db.conn:
        db.conn.execute("UPDATE report_cache SET report_json=? WHERE report_id=?",
                        (json.dumps(stored), report["report_id"]))
    content = _json(client.get(f"/api/v1/reports/{report['report_id']}/content"))
    assert content["rule_guidance"]["source"] == "rule"
    assert content["rule_guidance"]["degraded_reason"] is None
    assert json.loads(db.get_report_row(report["report_id"])["report_json"]) == stored


def test_note_closed_while_the_model_is_running_is_not_returned_as_a_reference(report_case, monkeypatch):
    report = report_case[2]
    db = get_database()
    athlete_id = report["athlete"]["id"]
    note_id = add_note(db, athlete_id, "goal", "仅本人保留", "athlete", "author")

    def finish_after_close(messages):
        assert f"note:{note_id}" in messages[0]["content"]
        assert close_note(db, athlete_id, note_id)
        return demo_responder(messages)

    use_mock(monkeypatch, MockLLMClient(responder=finish_after_close))
    result = _json(_interpret(report_case))
    _assert_fallback(result, "error", report)
    assert f"note:{note_id}" not in {ref["ref"] for ref in result["references"]}
