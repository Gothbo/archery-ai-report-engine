# -*- coding: utf-8 -*-
"""B1-7：LLM 客户端测试（mock 应答 / HTTP 协议 / 重试 / 流式拒绝 / 工厂）。"""
import pytest

from app.config import LLMConfig
from app.llm.client import HTTPLLMClient, LLMError, MockLLMClient, get_llm_client


def _cfg(**over) -> LLMConfig:
    return LLMConfig(**over)


class TestMockClient:
    def test_fixed_responses_pop(self):
        c = MockLLMClient(responses=["甲", "乙"])
        assert c.chat([{"role": "user", "content": "q"}]) == "甲"
        assert c.chat([{"role": "user", "content": "q"}]) == "乙"

    def test_no_responses_raises(self):
        with pytest.raises(LLMError):
            MockLLMClient().chat([])

    def test_responder_invoked_with_messages(self):
        seen = []
        c = MockLLMClient(responder=lambda msgs: seen.append(msgs) or "ok")
        msgs = [{"role": "user", "content": "hi"}]
        assert c.chat(msgs) == "ok"
        assert seen == [msgs]

    def test_error_passthrough(self):
        c = MockLLMClient(error=LLMError("boom"))
        with pytest.raises(LLMError):
            c.chat([])


class TestHTTPClient:
    def test_llamacpp_openai_payload(self, monkeypatch):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "回答"}}]}

        class FakeClient:
            def __init__(self, timeout, trust_env=True):
                captured["timeout"] = timeout

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()

        monkeypatch.setattr("httpx.Client", FakeClient)
        c = HTTPLLMClient(_cfg(provider="llamacpp", endpoint="http://127.0.0.1:8080"))
        assert c.chat([{"role": "user", "content": "q"}]) == "回答"
        assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
        assert captured["json"]["stream"] is False
        assert captured["json"]["temperature"] == 0.2

    def test_ollama_payload(self, monkeypatch):
        captured = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": "答"}}

        class FakeClient:
            def __init__(self, timeout, trust_env=True):
                captured["timeout"] = timeout

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()

        monkeypatch.setattr("httpx.Client", FakeClient)
        c = HTTPLLMClient(_cfg(provider="ollama", endpoint="http://127.0.0.1:11434",
                               temperature=0.7, num_ctx=4096))
        assert c.chat([{"role": "user", "content": "q"}]) == "答"
        assert captured["url"] == "http://127.0.0.1:11434/api/chat"
        assert captured["json"]["stream"] is False
        assert captured["json"]["options"] == {"temperature": 0.7, "num_ctx": 4096}

    def test_empty_content_raises(self, monkeypatch):
        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "  "}}]}

        class FakeClient:
            def __init__(self, timeout):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                return FakeResp()

        monkeypatch.setattr("httpx.Client", FakeClient)
        c = HTTPLLMClient(_cfg(provider="llamacpp"))
        with pytest.raises(LLMError):
            c.chat([])

    def test_retry_then_raise(self, monkeypatch):
        calls = {"n": 0}

        class BoomResp:
            def raise_for_status(self):
                raise RuntimeError("connection refused")

        class FakeClient:
            def __init__(self, timeout, trust_env=True):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json):
                calls["n"] += 1
                return BoomResp()

        monkeypatch.setattr("httpx.Client", FakeClient)
        c = HTTPLLMClient(_cfg(provider="llamacpp", max_retries=2))
        with pytest.raises(LLMError) as ei:
            c.chat([])
        assert calls["n"] == 3  # 1 次 + 2 次重试
        assert "重试 2 次后" in str(ei.value)

    def test_stream_raises_d3(self):
        c = HTTPLLMClient(_cfg(provider="llamacpp"))
        with pytest.raises(LLMError) as ei:
            c.chat([], stream=True)
        assert "流式未启用" in str(ei.value)


class TestFactory:
    def test_mock_provider(self):
        c = get_llm_client(_cfg(provider="mock"))
        assert isinstance(c, MockLLMClient)

    def test_http_providers(self):
        assert isinstance(get_llm_client(_cfg(provider="llamacpp")), HTTPLLMClient)
        assert isinstance(get_llm_client(_cfg(provider="ollama")), HTTPLLMClient)
