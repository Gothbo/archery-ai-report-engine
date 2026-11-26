"""Feedback attribution, review rounds, and the interpretation quality gate."""
from datetime import datetime, timedelta, timezone

import pytest

from app.api import guidance
from app.llm.client import MockLLMClient
from app.store.database import Database, get_database
from tests.test_desktop_ingest import URL as INGEST, _shot, _snapshot
from tests.test_guidance_interpret import _interpret, _json, report_case
from tests.test_guidance_stream import use_mock
from tests.test_rule_packs import _draft

SUMMARY = "/api/v1/guidance/feedback/summary"
ADMIN = {"Authorization": "Bearer fixture-admin"}


@pytest.fixture()
def case(report_case, monkeypatch):
    monkeypatch.setenv("ENGINE_RULE_PACK_ADMIN_TOKEN", "fixture-admin")
    return report_case


def _body(report, **overrides):
    return {
        "report_id": report["report_id"], "granularity": report["granularity"],
        "window_key": report["window_key"], "view": "coach",
        "rule_pack_version": (report["rule_pack"] or {}).get("version", ""),
        "source": "rule", "rating": 4, "will_follow": True, "reason": "安排清楚",
        "rules": [b["source"] for b in report["rule_guidance"]["bullets"]],
        "actor_id": "desktop:fixture",
        "submitted_at_utc": datetime.now(timezone.utc).isoformat(),
        **overrides,
    }


def _submit(case, data):
    return case[0].post(f"{case[1]}/guidance/feedback", json=data)


def _summary(case, **params):
    return _json(case[0].get(SUMMARY, headers=ADMIN, params=params))


def _second_report(case, day):
    client, base, *_ = case
    # Keep the original calendar report valid while exercising independent report rounds.
    snapshot = _snapshot(session_key=f"feedback-{day}", start=f"{day}T10:00:00",
                         shots=[_shot(i, ts=f"{day}T10:{i:02d}:00") for i in range(1, 7)])
    snapshot["session"]["endedAt"] = f"{day}T11:00:00"
    receipt = _json(client.post(INGEST, json=snapshot))
    return _json(client.post(f"{base}/reports/daily", params={"session_id": receipt["sessionId"]}))


def _rule(summary, name):
    return next(row for row in summary["rules"] if row["rule"] == name)


def test_feedback_overwrites_by_report_and_actor_without_rewriting_snapshot(case):
    report = case[2]
    db = get_database()
    original = dict(db.get_report_row(report["report_id"]))
    initial = _body(report, source="model", rating=2)
    accepted = _json(_submit(case, initial))
    assert accepted["accepted"] is True and accepted["id"]

    update = {**initial, "window_key": "2026-11-19", "view": "athlete", "source": "rule",
              "rating": 5, "will_follow": False}
    assert _json(_submit(case, update)) == accepted
    rows = db.query("SELECT * FROM guidance_feedback WHERE report_id=?", (report["report_id"],))
    assert len(rows) == 1
    row = rows[0]
    assert row["window_key"] == report["window_key"]
    assert row["rule_pack_version"] == update["rule_pack_version"]
    assert (row["view"], row["source"], row["rating"], row["will_follow"]) == ("athlete", "rule", 5, 0)
    assert row["submitted_at_utc"].endswith("Z")
    assert _summary(case)["total"] == 1
    assert dict(db.get_report_row(report["report_id"])) == original


@pytest.mark.parametrize(("field", "value", "status"), [
    ("rating", 0, 422), ("rating", 6, 422), ("rating", True, 422),
    ("report_id", None, 422), ("actor_id", None, 422), ("source", "human", 422),
    ("report_id", "missing", 404), ("window_key", "daily:day:2026-11-20", 404),
    ("rule_pack_version", "invented", 422), ("rules", ["invented"], 422),
    ("reason", "字" * 121, 422), ("reason", "联系 13500138000", 422),
    ("reason", "示例运动员甲准备不足", 422),
    ("submitted_at_utc", "2026-11-26T03:00:00", 422), ("submitted_at_utc", "20261126", 422),
])
def test_invalid_feedback(case, field, value, status):
    data = _body(case[2])
    if value is None:
        data.pop(field)
    else:
        data[field] = value
    assert _submit(case, data).status_code == status


def test_summary_distinguishes_hits_ratings_and_two_distinct_low_rounds(case):
    first = case[2]
    second = _second_report(case, "2026-11-20")
    third = _second_report(case, "2026-11-21")
    rule = next(b["source"] for b in first["rule_guidance"]["bullets"] if b["source"] != "caveat_sample")
    assert case[0].get(SUMMARY).status_code == 401
    before = _summary(case)
    assert before["total"] == 0 and before["will_follow_rate"] is None
    assert _rule(before, rule)["hits"] == 3 and _rule(before, rule)["avg_rating"] is None

    now = datetime.now(timezone.utc)
    _json(_submit(case, _body(first, rules=[rule], rating=1, reason=None, will_follow=False,
                            submitted_at_utc=(now - timedelta(seconds=4)).isoformat())))
    _json(_submit(case, _body(first, rules=[rule], actor_id="desktop:another", rating=2, reason="不相关",
                            submitted_at_utc=(now - timedelta(seconds=3)).isoformat())))
    assert _summary(case)["review_queue"] == []
    _json(_submit(case, _body(second, rules=[rule, rule], rating=2, reason="不相关",
                            submitted_at_utc=(now - timedelta(seconds=2)).isoformat())))

    summary = _summary(case)
    row = _rule(summary, rule)
    assert (row["hits"], row["rated"], row["low_streak"], row["in_review"]) == (3, 3, 2, True)
    assert row["avg_rating"] == pytest.approx(5 / 3)
    assert row["will_follow_rate"] == pytest.approx(2 / 3)
    assert summary["low_reason_distribution"] == {"": 1, "不相关": 2}
    item = summary["review_queue"][0]
    assert item["rule_pack_version"] == _body(first)["rule_pack_version"]
    assert [s["report_id"] for s in item["samples"]] == [second["report_id"], first["report_id"]]

    _json(_submit(case, _body(third, rules=[rule], rating=3, reason="较好",
                            submitted_at_utc=(now - timedelta(milliseconds=2500)).isoformat())))
    assert _summary(case)["review_queue"] == []
    assert "较好" not in _summary(case)["low_reason_distribution"]
    empty = _summary(case, **{"from": "2020-01-01", "to": "2020-01-02"})
    assert empty == {"total": 0, "avg_rating": None, "will_follow_rate": None,
                     "rules": [], "low_reason_distribution": {}, "review_queue": []}


def test_report_replacement_keeps_recorded_ratings_and_rule_hits(case):
    client, base, report, *_ = case
    _json(_submit(case, _body(report)))
    day = report["window_key"].split(":")[-1]
    refreshed = _json(client.post(f"{base}/reports/daily",
                                  json={"day": day, "session_ids": report["session_ids"]}))
    assert refreshed["report_id"] != report["report_id"]
    assert client.get(f"/api/v1/reports/{report['report_id']}/content").status_code == 404
    summary = _summary(case)
    assert summary["total"] == 1
    assert all(rule["hits"] == 2 and rule["rated"] == 1 for rule in summary["rules"])


def test_upgrade_backfills_saved_report_hits_without_writing_report_body(case):
    report = case[2]
    db = get_database()
    before = dict(db.get_report_row(report["report_id"]))
    with db.lock, db.conn:
        db.conn.execute("DELETE FROM guidance_report_observations")
    reopened = Database(db.db_path)
    reopened.close()
    assert all(row["hits"] == 1 for row in _summary(case)["rules"])
    assert _summary(case)["rules"]
    assert dict(db.get_report_row(report["report_id"])) == before


def test_low_rounds_are_not_combined_across_rule_pack_versions(case):
    client, _, old_report, *_ = case
    version = "feedback-trial-2"
    _json(client.put(f"/api/v1/rule-packs/{version}", json=_draft(client), headers=ADMIN))
    _json(client.post(f"/api/v1/rule-packs/{version}/publish",
                      json={"actor_id": "fixture-admin"}, headers=ADMIN))
    new_report = _second_report(case, "2026-11-20")
    for report in (old_report, new_report):
        _json(_submit(case, _body(report, rating=2)))
    summary = _summary(case)
    assert summary["review_queue"] == []
    assert all(row["low_streak"] == 1 for row in summary["rules"])


@pytest.mark.parametrize(("text", "missing"), [
    ("五、提升方案\n", ["next_step"]),
    ("依据报告说明现状。", ["next_step"]),
    ("五、提升方案\n本期平均环是 987.321 环。", ["numbers"]),
    ("五、提升方案\n按报告安排练习。", ["references"]),
])
def test_interpret_quality_gate_withholds_failed_text(case, monkeypatch, text, missing):
    if missing == ["references"]:
        original = guidance._interpret_context
        monkeypatch.setattr(guidance, "_interpret_context",
                            lambda *args: {**original(*args), "references": []})
    use_mock(monkeypatch, MockLLMClient(responses=[text]))
    result = _json(_interpret(case))
    assert result["source"] == "rule" and result["degraded_reason"] == "quality_failed"
    assert result["quality_gate"] == {"passed": False, "missing": missing}
    assert result["text"].startswith(result["rule_guidance"]["summary"])
    assert result["sections"] == [] and result["model_version"] is None


def test_interpret_returns_a_passed_gate_for_actionable_model_text(case):
    result = _json(_interpret(case))
    assert result["source"] == "model"
    assert result["quality_gate"] == {"passed": True, "missing": []}
