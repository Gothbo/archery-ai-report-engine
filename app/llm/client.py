# -*- coding: utf-8 -*-
"""LLM 客户端（B1-2）：可替换接口，provider 适配（llamacpp / ollama）+ Mock。

- LLMClient 抽象：chat(messages, stream=False) -> str；astream_chat(messages) -> 异步逐段产出文本
- HTTPLLMClient：httpx 同步调用；llamacpp 走 OpenAI 兼容 /v1/chat/completions，
  ollama 走 /api/chat；超时 + 重试；model_version 透传（上下文版本键）
  流式（PR #3）：httpx.AsyncClient 流式读取（llama-server SSE / ollama NDJSON），不重试；
  关闭生成器 = 关闭上游 HTTP 连接，llama-server 检测到断开后停止生成（真取消；
  读取阶段要等当前 prompt 处理完，取消后由 wait_idle 轮询 /slots 确认空闲）
- MockLLMClient：测试/演示用，不绑真实模型；支持流式模式（可注入节奏/挂起/中途失败）
- 判读留规则引擎：本模块只做"表达"，不做任何数值判定
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
from typing import AsyncIterator

import httpx

from app.config import LLMConfig

logger = logging.getLogger("engine.llm.client")


class LLMError(Exception):
    """LLM 调用失败（连接/超时/协议/重试耗尽）→ ask 层降级模板。"""


class LLMOfflineError(LLMError):
    """连不上模型服务（流式路径）→ COACH-OFFLINE（前端降级规则版）。"""


class LLMClient(abc.ABC):
    """可替换接口：换 provider / 换模型对 ask 层零侵入。"""

    @abc.abstractmethod
    def chat(self, messages: list[dict], stream: bool = False) -> str:
        """messages=[{role, content}, ...]；返回助手文本。stream 签名预留（D3 单轮非流式）。"""
        raise NotImplementedError

    async def astream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式：异步逐段产出助手文本（不重试）。调用方 aclose()/取消 = 停止上游。"""
        raise LLMError("该 LLM 客户端不支持流式")
        yield ""  # pragma: no cover  （使本函数成为异步生成器）


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

    async def astream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        """流式调用（PR #3）。超时由调用方（app/llm/tasks.py）按首 token / 空闲 / 总截止控制；
        这里的 httpx 超时只是兜底（连接用 connect_timeout_sec，读取用 total_timeout_sec）。

        退出路径（正常结束 / 异常 / 被取消 / 被 aclose）都会经过 async with 退出，
        关闭上游响应与连接；llama-server 检测到断开后取消该任务（撰写阶段实测约 0.05 s；
        读取阶段要等当前 prompt 处理完，见 wait_idle）。
        """
        cfg = self.cfg
        timeout = httpx.Timeout(cfg.total_timeout_sec, connect=cfg.connect_timeout_sec)
        if cfg.provider == "ollama":
            url = f"{cfg.endpoint.rstrip('/')}/api/chat"
            payload = {
                "model": cfg.model,
                "messages": messages,
                "stream": True,
                "options": {"temperature": cfg.temperature, "num_ctx": cfg.num_ctx},
            }
        else:  # llamacpp：llama-server 原生 OpenAI 兼容 SSE
            url = f"{cfg.endpoint.rstrip('/')}/v1/chat/completions"
            payload = {
                "model": cfg.model,
                "messages": messages,
                "stream": True,
                "temperature": cfg.temperature,
            }
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")[:300]
                        raise LLMError(f"模型服务返回 HTTP {resp.status_code}：{body}")
                    async for line in resp.aiter_lines():
                        piece, done = _parse_stream_line(cfg.provider, line)
                        if piece:
                            yield piece
                        if done:
                            return
        except httpx.ConnectError as exc:
            raise LLMOfflineError(f"连不上模型服务 {cfg.endpoint}：{exc}") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"模型服务流式读取失败：{type(exc).__name__} {exc}") from exc

    async def wait_idle(self, max_wait: float) -> bool | None:
        """取消/超时后确认模型服务真的空闲：轮询 llama-server `GET /slots`（默认开启），
        直到没有 slot 在处理或到 max_wait。返回 True=已空闲 / False=等到上限仍忙 / None=无法判断。

        实测（llama.cpp b11435，CPU）：撰写阶段断开后约 0.05 s 停止；但读取阶段（prefill）断开后，
        服务端要把当前 prompt 处理完才执行取消。所以引擎在这段时间保持 cancelling（新请求仍 409），
        不把「连接已关」当成「模型已停」。
        """
        if self.cfg.provider != "llamacpp":
            return None
        url = f"{self.cfg.endpoint.rstrip('/')}/slots"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), trust_env=False) as client:
            while True:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        return None
                    busy = any(s.get("is_processing") for s in resp.json())
                except Exception:  # noqa: BLE001 —— 拿不到状态就不等（不能因此卡住锁）
                    return None
                if not busy:
                    return True
                if loop.time() >= deadline:
                    return False
                await asyncio.sleep(0.2)


def _parse_stream_line(provider: str, line: str) -> tuple[str, bool]:
    """解析一行流式输出 → (文本片段, 是否结束)。
    llamacpp：SSE 行 `data: {...}` / `data: [DONE]`；ollama：NDJSON `{"message":{"content":..},"done":..}`。"""
    line = line.strip()
    if not line:
        return "", False
    if provider == "ollama":
        obj = json.loads(line)
        if obj.get("error"):
            raise LLMError(f"模型服务报错：{obj['error']}")
        return (obj.get("message") or {}).get("content") or "", bool(obj.get("done"))
    if not line.startswith("data:"):
        return "", False  # SSE 注释 / event: 行等，忽略
    data = line[5:].strip()
    if data == "[DONE]":
        return "", True
    obj = json.loads(data)
    if obj.get("error"):
        raise LLMError(f"模型服务报错：{obj['error']}")
    choice = (obj.get("choices") or [{}])[0]
    piece = (choice.get("delta") or {}).get("content") or ""
    return piece, False


class MockLLMClient(LLMClient):
    """测试/演示用：固定响应列表按次弹出；也可用 responder 函数按提问动态生成。"""

    def __init__(self, responses: list[str] | None = None,
                 responder=None, error: Exception | None = None, *,
                 chunk_size: int = 4, chunk_delay: float = 0.0, first_token_delay: float = 0.0,
                 hang_after: int | None = None, fail_after: int | None = None):
        self._responses = list(responses or [])
        self._responder = responder
        self._error = error
        self.calls: list[list[dict]] = []
        # 流式模式参数：按 chunk_size 个字切片；首 token 前等 first_token_delay；
        # hang_after=N → 发完 N 片后挂起（模拟模型卡死，用于超时/取消测试）；
        # fail_after=N → 发完 N 片后抛 LLMError（模拟上游中途断流）
        self.chunk_size = max(1, chunk_size)
        self.chunk_delay = chunk_delay
        self.first_token_delay = first_token_delay
        self.hang_after = hang_after
        self.fail_after = fail_after
        # 观测点：上游是否开始 / 是否已关闭（取消、超时、断开后必须为 True）/ 已发片数
        self.stream_started = False
        self.stream_closed = False
        self.stream_chunks_sent = 0

    def chat(self, messages: list[dict], stream: bool = False) -> str:
        self.calls.append(messages)
        if self._error is not None:
            raise self._error
        if self._responder is not None:
            return self._responder(messages)
        if not self._responses:
            raise LLMError("MockLLMClient 无可用响应")
        return self._responses.pop(0)

    async def astream_chat(self, messages: list[dict]) -> AsyncIterator[str]:
        self.stream_started = True
        try:
            if self.first_token_delay:
                await asyncio.sleep(self.first_token_delay)
            text = self.chat(messages)
            for i in range(0, len(text), self.chunk_size):
                if self.hang_after is not None and self.stream_chunks_sent >= self.hang_after:
                    await asyncio.Event().wait()  # 永不返回，只能被取消/超时打断
                if self.fail_after is not None and self.stream_chunks_sent >= self.fail_after:
                    raise LLMError("mock 上游中途断流")
                if self.chunk_delay and self.stream_chunks_sent:
                    await asyncio.sleep(self.chunk_delay)
                self.stream_chunks_sent += 1
                yield text[i:i + self.chunk_size]
            if self.hang_after is not None and self.stream_chunks_sent >= self.hang_after:
                await asyncio.Event().wait()
        finally:
            self.stream_closed = True


def get_llm_client(cfg: LLMConfig) -> LLMClient:
    """工厂：按 provider 构造。mock = 骨架取数的演示应答器（G2 恒通过）；测试 monkeypatch 本函数注入。"""
    if cfg.provider == "mock":
        from app.llm.mock import demo_responder
        return MockLLMClient(responder=demo_responder)
    return HTTPLLMClient(cfg)


# ---------------------------------------------------------------------------
# 模型服务可达性探测（PR #4：GET /llm/status 的 backend 字段）
# ---------------------------------------------------------------------------

PROBE_TIMEOUT_SEC = 1.0   # 探测超时：本机服务 1 s 内不应答即视为不可达
PROBE_TTL_SEC = 3.0       # 结果缓存：页面轮询不会把请求打到模型服务上
_PROBE_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_PROBE_TRANSPORT: httpx.AsyncBaseTransport | None = None  # 测试注入点（None = 真实网络）


def reset_backend_probe_cache() -> None:
    _PROBE_CACHE.clear()


async def probe_backend(cfg: LLMConfig, tz: str) -> dict:
    """返回 {"backend": "ready"|"loading"|"unreachable"|None, "backend_checked_at": str|None}。

    - llm.enabled=false：不探测，两项都是 None
    - mock：ready
    - llamacpp：GET {endpoint}/health —— 200 ready / 503 loading（模型加载中）/ 其他或连不上 unreachable
      （llama-server 文档语义；现场 b11435 构建待实测）
    - ollama：GET {endpoint}/api/version —— 200 ready，否则 unreachable（ollama 没有「加载中」状态）
    探测失败绝不抛异常，不影响报告链路。
    """
    if not cfg.enabled:
        return {"backend": None, "backend_checked_at": None}
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    key = (cfg.provider, cfg.endpoint)
    hit = _PROBE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < PROBE_TTL_SEC:
        return dict(hit[1])
    if cfg.provider == "mock":
        backend = "ready"
    else:
        path = "/health" if cfg.provider == "llamacpp" else "/api/version"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(PROBE_TIMEOUT_SEC), trust_env=False,
                                         transport=_PROBE_TRANSPORT) as client:
                resp = await client.get(f"{cfg.endpoint.rstrip('/')}{path}")
            if resp.status_code == 200:
                backend = "ready"
            elif resp.status_code == 503 and cfg.provider == "llamacpp":
                backend = "loading"
            else:
                backend = "unreachable"
        except Exception as exc:  # noqa: BLE001 —— 连不上 / 超时 / 协议错误都算不可达
            logger.debug("模型服务探测失败 %s：%s", cfg.endpoint, exc)
            backend = "unreachable"
    result = {"backend": backend,
              "backend_checked_at": datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")}
    _PROBE_CACHE[key] = (time.monotonic(), result)
    return dict(result)
