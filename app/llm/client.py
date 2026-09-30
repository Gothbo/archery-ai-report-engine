# -*- coding: utf-8 -*-
"""LLM 客户端（B1-2）：可替换接口，provider 适配（llamacpp / ollama）+ Mock。

- LLMClient 抽象：chat(messages, stream=False) -> str
- HTTPLLMClient：httpx 同步调用；llamacpp 走 OpenAI 兼容 /v1/chat/completions，
  ollama 走 /api/chat；超时 + 重试；model_version 透传（上下文版本键）
- MockLLMClient：测试/演示用，不绑真实模型
- 判读留规则引擎：本模块只做"表达"，不做任何数值判定
"""
from __future__ import annotations

import abc
import logging

import httpx

from app.config import LLMConfig

logger = logging.getLogger("engine.llm.client")


class LLMError(Exception):
    """LLM 调用失败（连接/超时/协议/重试耗尽）→ ask 层降级模板。"""


class LLMClient(abc.ABC):
    """可替换接口：换 provider / 换模型对 ask 层零侵入。"""

    @abc.abstractmethod
    def chat(self, messages: list[dict], stream: bool = False) -> str:
        """messages=[{role, content}, ...]；返回助手文本。stream 签名预留（D3 单轮非流式）。"""
        raise NotImplementedError


class HTTPLLMClient(LLMClient):
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._timeout = httpx.Timeout(cfg.timeout_sec)

    def _client(self) -> httpx.Client:
        # trust_env=False：本地 llamacpp/ollama 必须直连；否则系统代理（VPN/远程软件）
        # 会拦截 127.0.0.1 请求并返回 502（本机实测复现）
        return httpx.Client(timeout=self._timeout, trust_env=False)

    def chat(self, messages: list[dict], stream: bool = False) -> str:
        if stream:
            raise LLMError("流式未启用（D3：MVP 单轮非流式）")
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                return self._chat_once(messages)
            except LLMError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < self.cfg.max_retries:
                    logger.warning("LLM 调用第 %d 次失败：%s，重试", attempt + 1, exc)
        raise LLMError(f"LLM 调用失败（重试 {self.cfg.max_retries} 次后）：{last_exc}")

    def _chat_once(self, messages: list[dict]) -> str:
        cfg = self.cfg
        if cfg.provider == "ollama":
            url = f"{cfg.endpoint.rstrip('/')}/api/chat"
            payload = {
                "model": cfg.model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": cfg.temperature, "num_ctx": cfg.num_ctx},
            }
            with self._client() as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            content = (data.get("message") or {}).get("content")
        else:  # llamacpp（OpenAI 兼容）
            url = f"{cfg.endpoint.rstrip('/')}/v1/chat/completions"
            payload = {
                "model": cfg.model,
                "messages": messages,
                "stream": False,
                "temperature": cfg.temperature,
            }
            with self._client() as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        if not content or not str(content).strip():
            raise LLMError("LLM 返回空内容")
        return str(content).strip()


class MockLLMClient(LLMClient):
    """测试/演示用：固定响应列表按次弹出；也可用 responder 函数按提问动态生成。"""

    def __init__(self, responses: list[str] | None = None,
                 responder=None, error: Exception | None = None):
        self._responses = list(responses or [])
        self._responder = responder
        self._error = error
        self.calls: list[list[dict]] = []

    def chat(self, messages: list[dict], stream: bool = False) -> str:
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        if self._responder is not None:
            return self._responder(messages)
        if not self._responses:
            raise LLMError("MockLLMClient 无可用响应")
        return self._responses.pop(0)


def get_llm_client(cfg: LLMConfig) -> LLMClient:
    """工厂：按 provider 构造。mock = 骨架取数的演示应答器（G2 恒通过）；测试 monkeypatch 本函数注入。"""
    if cfg.provider == "mock":
        from app.llm.mock import demo_responder
        return MockLLMClient(responder=demo_responder)
    return HTTPLLMClient(cfg)
