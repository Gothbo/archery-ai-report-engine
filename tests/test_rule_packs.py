import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import REAL_CONFIG, _make_temp_config, _reset_engine
from tests.test_desktop_ingest import URL as INGEST, _snapshot

PACKS = "/api/v1/rule-packs"
DEFAULT = "v1-trial-20261010"
ADMIN = {"Authorization": "Bearer test-publish-credential"}
SIGNER = {"Authorization": "Bearer test-signing-credential"}


@pytest.fixture()
def client(engine_env, tmp_path, monkeypatch):
    from app.config import get_config
    from app.main import app

    _make_temp_config(tmp_path, mdc_source=DEFAULT)
    get_config.cache_clear()
    monkeypatch.setenv("ENGINE_RULE_PACK_ADMIN_TOKEN", "test-publish-credential")
    monkeypatch.setenv("ENGINE_RULE_PACK_SIGNER_TOKEN", "test-signing-credential")
    return TestClient(app)


def _json(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


def _draft(client):
    seed = _json(client.get(f"{PACKS}/{DEFAULT}"))
    return {"actor_id": "operator-1", "rules": seed["rules"],
            "sources": [{"title": "试行复核记录", "reference": "internal:review-2"}],
            "review_date": "2027-02-01"}


def test_seed_migrates_current_values_without_claiming_signature(client):
    existing = json.loads(REAL_CONFIG.read_text(encoding="utf-8"))
    listing = _json(client.get(PACKS))
    assert listing["active_version"] == DEFAULT
    pack = _json(client.get(f"{PACKS}/{DEFAULT}"))
    assert pack["status"] == "trial"
    assert pack["signed_by"] is None and pack["signed_at_utc"] is None
    assert pack["sources"] and pack["review_date"] and pack["effective_at_utc"]
    assert pack["updated_by"]
    for name, threshold in existing["mdc"].items():
        actual = pack["rules"]["mdc"][name]
        assert "direction" not in actual
        assert all(actual[key] == value for key, value in threshold.items() if key != "direction")
    assert pack["rules"]["wind_bands"] == existing["wind_bands"]
    assert pack["rules"]["min_wind_band_shots"] == existing["advice"]["min_band_shots"]
    assert all(pack["rules"]["sample_threshold"][key] == value
               for key, value in existing["sample_threshold"].items() if not key.startswith("_"))


@pytest.mark.parametrize(("path", "invalid"), [
    (("mdc", "avgScore", "threshold"), -1),
    (("mdc", "avgScore", "swc"), -0.1),
    (("mdc", "avgScore", "direction"), -1),
    (("wind_bands",), []),
    (("wind_bands",), [[0, 1], [2, 3]]),
    (("min_wind_band_shots",), 0),
    (("sample_threshold", "weekly"), 0),
    (("advice_templates", "inner"), "{inner.__class__}"),
])
def test_invalid_rule_values_cannot_be_saved_or_published(client, path, invalid):
    draft = _draft(client)
    field = draft["rules"]
    for key in path[:-1]:
        field = field[key]
    field[path[-1]] = invalid
    response = _json(client.put(f"{PACKS}/invalid-v2", json=draft, headers=ADMIN), 422)
    assert response["detail"]
    _json(client.post(f"{PACKS}/invalid-v2/publish", json={"actor_id": "operator-1"}, headers=ADMIN), 404)
    assert _json(client.get(PACKS))["active_version"] == DEFAULT


def test_publish_binds_calculation_and_preserves_existing_report_and_signing_is_metadata_only(client):
    from app.store.database import get_database

    ingest = _json(client.post(INGEST, json=_snapshot()))
    generate = f"/api/v1/athletes/{ingest['athleteId']}/reports/daily"
    query = {"session_id": ingest["sessionId"]}
    old = _json(client.post(generate, params=query))
    assert old["rule_pack"]["version"] == DEFAULT
    assert old["rule_pack"]["status"] == "trial"
    assert old["rule_guidance"]["rule_pack"] == old["rule_pack"]

    version = "v2-trial-test"
    draft = _draft(client)
    draft["rules"]["sample_threshold"]["daily"] = 1
    draft["rules"]["advice_templates"]["inner"] = "内十观察（{inner}%）"
    _json(client.put(f"{PACKS}/{version}", json=draft), 401)
    saved = _json(client.put(f"{PACKS}/{version}", json=draft, headers=ADMIN))
    assert saved["status"] == "draft" and saved["effective_at_utc"] is None
    assert _json(client.post(generate, params=query))["report_id"] == old["report_id"]
    _json(client.post(f"{PACKS}/{version}/sign", json={"actor_id": "reviewer"}, headers=SIGNER), 409)
    published = _json(client.post(f"{PACKS}/{version}/publish", json={"actor_id": "publisher"}, headers=ADMIN))
    assert published["status"] == "trial" and published["effective_at_utc"]
    _json(client.put(f"{PACKS}/{version}", json=draft, headers=ADMIN), 409)

    updated = _json(client.post(generate, params=query))
    assert updated["report_id"] != old["report_id"]
    assert updated["rule_pack"]["version"] == version
    assert updated["rule_pack"]["sources"] == draft["sources"]
    assert updated["suggestions"] == ["内十观察（0.0%）"]
    assert updated["rule_guidance"]["bullets"][0]["text"] == updated["suggestions"][0]
    persisted = dict(get_database().get_report_row(updated["report_id"]))
    assert persisted["rule_pack_version"] == version
    old_content = f"/api/v1/reports/{old['report_id']}/content"
    assert _json(client.get(old_content))["sections"] == old["sections"]
    assert _json(client.get(old_content))["rule_pack"] == old["rule_pack"]

    signed_path = f"{PACKS}/{version}/sign"
    _json(client.post(signed_path, json={"actor_id": "reviewer"}, headers=ADMIN), 403)
    signed = _json(client.post(signed_path, json={"actor_id": "reviewer"}, headers=SIGNER))
    assert signed["status"] == "signed" and signed["signed_by"] == "reviewer"
    assert signed["rules"] == published["rules"]
    assert signed["effective_at_utc"] == published["effective_at_utc"]
    cached = _json(client.get(f"/api/v1/reports/{updated['report_id']}/content"))
    assert cached["sections"] == updated["sections"]
    assert cached["rule_pack"]["status"] == "trial"
    assert cached["rule_pack"]["current_status"] == "signed"
    assert cached["rule_pack"]["signed_at_utc"] == signed["signed_at_utc"]
    assert dict(get_database().get_report_row(updated["report_id"])) == persisted

    get_database().close()
    _reset_engine()
    assert _json(client.get(PACKS))["active_version"] == version
    assert _json(client.get(old_content))["sections"] == old["sections"]

    fresh = _json(client.post(generate, params={**query, "refresh": True}))
    assert fresh["rule_pack"]["status"] == "signed"
    assert fresh["rule_guidance"]["rule_pack"] == fresh["rule_pack"]
    audit = _json(client.get(f"{PACKS}/{version}/audit"))["events"]
    assert [(event["action"], event["actor_id"]) for event in audit] == [
        ("publish", "publisher"), ("sign", "reviewer"),
    ]
    assert all(event["version"] == version and event["occurred_at_utc"] for event in audit)


def test_rule_pack_writes_fail_closed_when_credentials_are_missing(client, monkeypatch):
    monkeypatch.delenv("ENGINE_RULE_PACK_ADMIN_TOKEN")
    _json(client.put(f"{PACKS}/local-draft", json=_draft(client), headers=ADMIN), 503)
    assert _json(client.get(PACKS))["active_version"] == DEFAULT


def test_streaming_guidance_returns_report_rule_pack_source(client, tmp_path, monkeypatch):
    from app.config import get_config
    from tests.test_guidance_stream import demo_mock, parse_sse, use_mock

    config = _make_temp_config(tmp_path, mdc_source=DEFAULT)
    config["llm"] = {"enabled": True, "provider": "mock", "model": "mock"}
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    get_config.cache_clear()
    use_mock(monkeypatch, demo_mock())
    ingest = _json(client.post(INGEST, json=_snapshot()))
    base = f"/api/v1/athletes/{ingest['athleteId']}"
    report = _json(client.post(f"{base}/reports/daily", params={"session_id": ingest["sessionId"]}))
    response = client.post(f"{base}/guidance/stream", json={
        "granularity": report["granularity"], "window_key": report["window_key"], "view": "coach",
    })
    assert response.status_code == 200, response.text
    event, result = parse_sse(response.text)[-1]
    assert event == "final"
    assert result["rule_pack"] == report["rule_pack"]
