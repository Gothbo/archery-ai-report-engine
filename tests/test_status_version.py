# -*- coding: utf-8 -*-
"""PR #4：/llm/status 的 provider / backend / backend_checked_at；/health 的 engine_version。"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.llm.client as clientmod
from app.config import LLMConfig
from app.main import app

BASE = Path(__file__).resolve().parent.parent


def _set_llm(tmp_path, **llm):
    import app.config as cfgmod
    p = tmp_path / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["llm"] = llm
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    cfgmod.get_config.cache_clear()
    clientmod.reset_backend_probe_cache()


@pytest.fixture()
def client(engine_env):
    return TestClient(app)


def _transport(status: int, calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, json={"status": "ok"} if status == 200 else {"error": {"message": "Loading model"}})
    return httpx.MockTransport(handler)


class TestLlmStatusBackend:
    def test_disabled_not_probed(self, client, monkeypatch):
        calls = []
        monkeypatch.setattr(clientmod, "_PROBE_TRANSPORT", _transport(200, calls))
        body = client.get("/api/v1/llm/status").json()
        assert body["llm_enabled"] is False
        assert body["backend"] is None and body["backend_checked_at"] is None
        assert body["provider"] == "llamacpp"
        assert calls == []
        # PR #3 原有字段不变
        assert {"busy", "state", "task_id", "kind", "stage", "started_at", "deadline_at"} <= set(body)

    @pytest.mark.parametrize("status,expected", [(200, "ready"), (503, "loading"), (500, "unreachable"),
                                                 (404, "unreachable")])
    def test_llamacpp_health_mapping(self, client, tmp_path, monkeypatch, status, expected):
        calls = []
        monkeypatch.setattr(clientmod, "_PROBE_TRANSPORT", _transport(status, calls))
        _set_llm(tmp_path, enabled=True, provider="llamacpp", endpoint="http://127.0.0.1:8090")
        body = client.get("/api/v1/llm/status").json()
        assert body["backend"] == expected and body["provider"] == "llamacpp"
        assert calls == ["http://127.0.0.1:8090/health"]
        assert body["backend_checked_at"].endswith("+08:00")

    def test_ollama_probe(self, client, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(clientmod, "_PROBE_TRANSPORT", _transport(503, calls))
        _set_llm(tmp_path, enabled=True, provider="ollama", endpoint="http://127.0.0.1:11434/")
        body = client.get("/api/v1/llm/status").json()
        assert body["backend"] == "unreachable"  # ollama 没有「加载中」
        assert calls == ["http://127.0.0.1:11434/api/version"]

    def test_mock_ready_without_network(self, client, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(clientmod, "_PROBE_TRANSPORT", _transport(500, calls))
        _set_llm(tmp_path, enabled=True, provider="mock", endpoint="http://127.0.0.1:1")
        assert client.get("/api/v1/llm/status").json()["backend"] == "ready"
        assert calls == []

    def test_connection_refused_unreachable(self, client, tmp_path):
        _set_llm(tmp_path, enabled=True, provider="llamacpp", endpoint="http://127.0.0.1:1")
        r = client.get("/api/v1/llm/status")
        assert r.status_code == 200 and r.json()["backend"] == "unreachable"

    def test_cached_between_polls(self, client, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(clientmod, "_PROBE_TRANSPORT", _transport(200, calls))
        _set_llm(tmp_path, enabled=True, provider="llamacpp", endpoint="http://127.0.0.1:8090")
        for _ in range(5):
            client.get("/api/v1/llm/status")
        assert len(calls) == 1
        monkeypatch.setattr(clientmod, "PROBE_TTL_SEC", 0.0)
        client.get("/api/v1/llm/status")
        assert len(calls) == 2

    def test_probe_timeout_never_raises(self, monkeypatch):
        def handler(request):
            raise httpx.ReadTimeout("slow", request=request)
        monkeypatch.setattr(clientmod, "_PROBE_TRANSPORT", httpx.MockTransport(handler))
        clientmod.reset_backend_probe_cache()
        cfg = LLMConfig(enabled=True, provider="llamacpp", endpoint="http://127.0.0.1:8090")
        out = asyncio.run(clientmod.probe_backend(cfg, "Asia/Shanghai"))
        assert out["backend"] == "unreachable"
        clientmod.reset_backend_probe_cache()


class TestHealthVersion:
    def test_engine_version_from_pyproject(self, client):
        tomllib = pytest.importorskip("tomllib")  # Python 3.11+
        expected = tomllib.loads((BASE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        body = client.get("/api/v1/health").json()
        assert body["engine_version"] == expected
        # 原有字段不变
        assert {"status", "config_version", "timezone", "db", "mdc_source", "llm_enabled"} <= set(body)

    def test_openapi_version_matches(self, client):
        assert client.get("/openapi.json").json()["info"]["version"] == client.get(
            "/api/v1/health").json()["engine_version"]
