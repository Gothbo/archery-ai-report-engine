# -*- coding: utf-8 -*-
"""PR #3：流式 AI 训练指导 + 单飞锁 + 服务端真取消。

- TestClient 会把整段流缓冲完再返回（starlette testclient 跑完 app 才返回），
  所以「自然结束」类用例（事件顺序 / 运动员无 delta / 护栏 / 超时 / 409）用 TestClient；
- 「中途取消 / 断开 / 并发」必须在流进行中操作，用线程里起的真 uvicorn + httpx 流式读取。
"""
from __future__ import annotations

import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import tests.conftest as ct
from app.main import app
from tests.conftest import ATHLETE
from tests.test_ask_api import _ingest_and_report

WEEK = "2026-W33"
HEADINGS = ("一、本次概述", "二、数据基础", "三、技术表现", "四、状态与负荷", "五、提升方案")


# ---------------------------------------------------------------------------
# 夹具 / 工具
# ---------------------------------------------------------------------------

def _write_cfg(tmp_path, monkeypatch, **llm_over):
    import app.config as cfgmod
    cfg = ct._make_temp_config(tmp_path)
    cfg["llm"] = {"enabled": True, "provider": "mock", "model": "mock",
                  "endpoint": "http://127.0.0.1:1", **llm_over}
    (tmp_path / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setenv("ENGINE_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
    ct._reset_engine()
    return cfg


@pytest.fixture()
def llm_cfg(tmp_path, monkeypatch):
    """返回一个函数：按需改 llm 配置（超时/上限/provider），并重置引擎状态。"""
    def make(**over):
        return _write_cfg(tmp_path, monkeypatch, **over)
    make()
    yield make
    ct._reset_engine()


@pytest.fixture()
def client(llm_cfg):
    c = TestClient(app)
    _ingest_and_report(c, week=WEEK)
    return c


def use_mock(monkeypatch, mock):
    import app.api.guidance as gmod
    monkeypatch.setattr(gmod, "get_llm_client", lambda cfg: mock)
    return mock


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        ev, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if ev:
            events.append((ev, data))
    return events


def post_guidance(c, view="coach", **over):
    body = {"granularity": "weekly", "window_key": WEEK, "view": view, **over}
    return c.post(f"/api/v1/athletes/{ATHLETE}/guidance/stream", json=body)


def names(events):
    return [e if e != "stage" else f"stage:{d['stage']}" for e, d in events]


def demo_mock(**kw):
    from app.llm.client import MockLLMClient
    from app.llm.mock import demo_responder
    return MockLLMClient(responder=demo_responder, **kw)


def fixed_mock(text, **kw):
    from app.llm.client import MockLLMClient
    return MockLLMClient(responses=[text], **kw)


# ---------------------------------------------------------------------------
# 事件协议（自然结束）
# ---------------------------------------------------------------------------

class TestEventProtocol:
    def test_coach_event_order_and_final(self, client, monkeypatch):
        mock = use_mock(monkeypatch, demo_mock())
        r = post_guidance(client, "coach")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        ev = parse_sse(r.text)
        n = names(ev)
        assert n[0] == "accepted"
        assert n[1] == "stage:reading"
        assert n[2] == "stage:writing"
        assert n[-2] == "stage:verifying"
        assert n[-1] == "final"
        assert set(n[3:-2]) == {"delta"} and len(n[3:-2]) >= 2
        acc = ev[0][1]
        assert acc["view"] == "coach" and acc["stream_deltas"] is True
        assert acc["granularity"] == "weekly" and acc["window_key"] == WEEK
        assert acc["task_id"] and acc["deadline_at"]
        assert all(d["task_id"] == acc["task_id"] for _, d in ev)
        final = ev[-1][1]
        draft = "".join(d["text"] for e, d in ev if e == "delta")
        assert final["text"] == draft.strip()
        assert final["guard"]["passed"] is True and final["degraded"] is False
        for h in HEADINGS:
            assert h in final["text"]
        assert final["sources"] and final["context_version"]
        assert mock.stream_closed is True
        assert client.get("/api/v1/llm/status").json()["busy"] is False

    def test_athlete_view_never_receives_deltas(self, client, monkeypatch):
        use_mock(monkeypatch, demo_mock())
        ev = parse_sse(post_guidance(client, "athlete").text)
        n = names(ev)
        assert "delta" not in n
        assert n == ["accepted", "stage:reading", "stage:writing", "stage:verifying", "final"]
        assert ev[0][1]["stream_deltas"] is False
        assert ev[-1][1]["view"] == "athlete"

    def test_athlete_input_excludes_coach_note(self, client, monkeypatch):
        from app.memory.notes import add_note
        from app.store.database import get_database
        add_note(get_database(), ATHLETE, "coach_note", "私密教练观察ZXQ", "coach", "c1")
        for view, expect in (("athlete", False), ("coach", True)):
            mock = use_mock(monkeypatch, fixed_mock("一、本次概述\n整体平稳。"))
            parse_sse(post_guidance(client, view).text)
            system = mock.calls[0][0]["content"]
            assert ("私密教练观察ZXQ" in system) is expect, view

    def test_rolling_baseline_in_both_views(self, client, monkeypatch):
        """拍板：滚动基线两个视角都带（不改现状）。"""
        for view in ("athlete", "coach"):
            mock = use_mock(monkeypatch, fixed_mock("一、本次概述\n整体平稳。"))
            ev = parse_sse(post_guidance(client, view).text)
            assert any(s["type"] == "baseline" for s in ev[-1][1]["sources"]), view

    def test_prompt_has_fixed_headings(self, client, monkeypatch):
        mock = use_mock(monkeypatch, fixed_mock("一、本次概述\n整体平稳。"))
        parse_sse(post_guidance(client, "coach").text)
        system = mock.calls[0][0]["content"]
        for h in HEADINGS:
            assert h in system


class TestGuardrailAndErrors:
    def test_guardrail_fail_degrades_whole_section(self, client, monkeypatch):
        from app.api.ask import DEGRADED_GUARD
        use_mock(monkeypatch, fixed_mock("一、本次概述\n本周平均环 99.9 环，进步 5.0 环。"))
        ev = parse_sse(post_guidance(client, "coach").text)
        n = names(ev)
        assert n[-1] == "guardrail_failed" and "final" not in n
        data = ev[-1][1]
        assert data["code"] == "COACH-GUARD" and data["rule"] == "G2"
        assert data["message"] == DEGRADED_GUARD
        assert data["fallback_skeleton"]
        assert "99.9" in data["reason"]  # 教练视角回显原因
        assert client.get("/api/v1/llm/status").json()["busy"] is False

    def test_guardrail_fail_athlete_shows_no_unverified_text(self, client, monkeypatch):
        use_mock(monkeypatch, fixed_mock("一、本次概述\n本周平均环 99.9 环，进步 5.0 环。"))
        r = post_guidance(client, "athlete")
        ev = parse_sse(r.text)
        assert "delta" not in names(ev)
        assert ev[-1][0] == "guardrail_failed"
        assert "reason" not in ev[-1][1]
        assert "99.9" not in r.text  # 未过护栏的文字/数字从未出现在运动员视角的流里

    def test_forbidden_phrase_g1(self, client, monkeypatch):
        use_mock(monkeypatch, fixed_mock("一、本次概述\n只要练它即提分。"))
        ev = parse_sse(post_guidance(client, "coach").text)
        assert ev[-1][0] == "guardrail_failed" and ev[-1][1]["rule"] == "G1"

    def test_too_long_stops_upstream_early_g3(self, client, monkeypatch, llm_cfg):
        llm_cfg(guidance_max_len=40)
        c = TestClient(app)
        text = "一、本次概述\n" + "节奏稳定动作一致" * 50
        mock = use_mock(monkeypatch, fixed_mock(text, chunk_size=4))
        ev = parse_sse(post_guidance(c, "coach").text)
        assert ev[-1][0] == "guardrail_failed" and ev[-1][1]["rule"] == "G3"
        assert mock.stream_closed is True
        assert mock.stream_chunks_sent < len(text) // 4  # 提前停止，没有读完上游

    def test_empty_output(self, client, monkeypatch):
        use_mock(monkeypatch, fixed_mock("   \n "))
        ev = parse_sse(post_guidance(client, "coach").text)
        assert ev[-1][0] == "error" and ev[-1][1]["code"] == "COACH-EMPTY"

    def test_upstream_fails_midway(self, client, monkeypatch):
        mock = use_mock(monkeypatch, demo_mock(fail_after=2))
        ev = parse_sse(post_guidance(client, "coach").text)
        assert ev[-1][0] == "error" and ev[-1][1]["code"] == "COACH-LLM"
        assert mock.stream_closed is True
        assert client.get("/api/v1/llm/status").json()["busy"] is False

    def test_offline_model_server(self, client, llm_cfg):
        llm_cfg(provider="llamacpp", endpoint=f"http://127.0.0.1:{_free_port()}", connect_timeout_sec=2)
        c = TestClient(app)
        ev = parse_sse(post_guidance(c, "coach").text)
        assert names(ev)[:2] == ["accepted", "stage:reading"]
        assert ev[-1][0] == "error" and ev[-1][1]["code"] == "COACH-OFFLINE"
        assert c.get("/api/v1/llm/status").json()["busy"] is False


class TestTimeouts:
    def test_first_token_timeout(self, client, monkeypatch, llm_cfg):
        llm_cfg(first_token_timeout_sec=0.3)
        c = TestClient(app)
        mock = use_mock(monkeypatch, demo_mock(hang_after=0))
        t0 = time.monotonic()
        ev = parse_sse(post_guidance(c, "coach").text)
        assert time.monotonic() - t0 < 5
        assert names(ev) == ["accepted", "stage:reading", "error"]
        assert ev[-1][1]["code"] == "COACH-TIMEOUT" and ev[-1][1]["timeout"] == "first_token"
        assert mock.stream_closed is True
        assert c.get("/api/v1/llm/status").json()["busy"] is False

    def test_inter_token_timeout(self, client, monkeypatch, llm_cfg):
        llm_cfg(idle_timeout_sec=0.3)
        c = TestClient(app)
        mock = use_mock(monkeypatch, demo_mock(hang_after=2))
        ev = parse_sse(post_guidance(c, "coach").text)
        n = names(ev)
        assert n.count("delta") == 2 and n[-1] == "error"
        assert ev[-1][1]["timeout"] == "idle"
        assert mock.stream_closed is True

    def test_total_deadline(self, client, monkeypatch, llm_cfg):
        llm_cfg(total_timeout_sec=0.5, idle_timeout_sec=5)
        c = TestClient(app)
        mock = use_mock(monkeypatch, demo_mock(chunk_size=1, chunk_delay=0.05))
        t0 = time.monotonic()
        ev = parse_sse(post_guidance(c, "coach").text)
        assert time.monotonic() - t0 < 3
        assert ev[-1][0] == "error" and ev[-1][1]["timeout"] == "total"
        assert mock.stream_closed is True
        assert c.get("/api/v1/llm/status").json()["busy"] is False


class TestPreStreamErrors:
    def test_llm_disabled_503(self, engine_env):
        c = TestClient(app)
        _ingest_and_report(c, week=WEEK)
        r = post_guidance(c, "coach")
        assert r.status_code == 503 and r.json()["code"] == "COACH-OFFLINE"

    def test_unknown_athlete_400_releases_lock(self, client):
        r = client.post("/api/v1/athletes/NOPE/guidance/stream",
                        json={"granularity": "weekly", "window_key": WEEK, "view": "coach"})
        assert r.status_code == 400
        assert client.get("/api/v1/llm/status").json()["busy"] is False

    def test_bad_granularity_422(self, client):
        r = post_guidance(client, "coach", granularity="hourly")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# 单飞锁 / 409 / 状态 / 取消接口（不需要流进行中）
# ---------------------------------------------------------------------------

def _occupy(kind="guidance", cancellable=True):
    from app.llm.tasks import get_registry
    return get_registry().try_acquire(kind=kind, view="coach", deadline_sec=60,
                                      tz="Asia/Shanghai", cancellable=cancellable)


class TestLockAndStatus:
    def test_status_idle_shape(self, client):
        body = client.get("/api/v1/llm/status").json()
        # PR #4 只追加 provider / backend / backend_checked_at，PR #3 原有字段与取值不变
        backend = {k: body.pop(k) for k in ("provider", "backend", "backend_checked_at")}
        assert body == {"busy": False, "state": "idle", "task_id": None, "kind": None, "stage": None,
                        "started_at": None, "deadline_at": None, "llm_enabled": True}
        assert backend["provider"] == "mock" and backend["backend"] == "ready" and backend["backend_checked_at"]

    def test_status_busy_has_no_athlete_identity(self, client):
        t = _occupy()
        body = client.get("/api/v1/llm/status").json()
        assert body["busy"] is True and body["state"] == "running" and body["task_id"] == t.task_id
        assert body["kind"] == "guidance" and body["deadline_at"]
        assert ATHLETE not in json.dumps(body)

    def test_guidance_409_when_busy(self, client, monkeypatch):
        mock = use_mock(monkeypatch, demo_mock())
        t = _occupy()
        r = post_guidance(client, "coach")
        assert r.status_code == 409
        body = r.json()
        assert body["code"] == "COACH-BUSY" and body["task_id"] == t.task_id
        assert body["state"] == "running" and body["retry_after_sec"] >= 1
        assert r.headers["Retry-After"] == str(body["retry_after_sec"])
        assert mock.stream_started is False  # 409 在打开流、调用模型之前返回

    def test_ask_409_when_busy(self, client):
        t = _occupy()
        r = client.post(f"/api/v1/athletes/{ATHLETE}/ask", json={"question": "这周怎么样？", "view": "coach"})
        assert r.status_code == 409 and r.json()["code"] == "COACH-BUSY"
        assert r.json()["task_id"] == t.task_id and "Retry-After" in r.headers

    def test_409_while_cancelling(self, client):
        from app.llm.tasks import get_registry
        t = _occupy()
        get_registry().request_cancel(t.task_id, "user")  # 无执行器：停在 cancelling
        r = post_guidance(client, "coach")
        assert r.status_code == 409
        assert r.json()["state"] == "cancelling" and r.headers["Retry-After"] == "2"
        assert client.get("/api/v1/llm/status").json()["state"] == "cancelling"

    def test_ask_takes_and_releases_lock(self, client, monkeypatch):
        from app.llm.client import LLMError, MockLLMClient
        from app.llm.tasks import get_registry
        import app.api.ask as askmod
        seen = {}

        def responder(msgs):
            seen["status"] = get_registry().status()
            return "整体平稳。"
        monkeypatch.setattr(askmod, "get_llm_client", lambda cfg: MockLLMClient(responder=responder))
        r = client.post(f"/api/v1/athletes/{ATHLETE}/ask", json={"question": "这周怎么样？", "view": "coach"})
        assert r.status_code == 200
        assert seen["status"]["busy"] is True and seen["status"]["kind"] == "ask"
        assert get_registry().status()["busy"] is False
        monkeypatch.setattr(askmod, "get_llm_client", lambda cfg: MockLLMClient(error=LLMError("x")))
        r = client.post(f"/api/v1/athletes/{ATHLETE}/ask", json={"question": "这周怎么样？", "view": "coach"})
        assert r.json()["degraded"] is True
        assert get_registry().status()["busy"] is False

    def test_cancel_unknown_task_404(self, client):
        r = client.post("/api/v1/llm/tasks/nope/cancel")
        assert r.status_code == 404 and r.json()["code"] == "COACH-TASK-NOT-FOUND"

    def test_cancel_ask_not_cancellable(self, client):
        t = _occupy(kind="ask", cancellable=False)
        r = client.post(f"/api/v1/llm/tasks/{t.task_id}/cancel")
        assert r.status_code == 409 and r.json()["code"] == "COACH-NOT-CANCELLABLE"
        assert client.get("/api/v1/llm/status").json()["state"] == "running"


# ---------------------------------------------------------------------------
# 真 uvicorn：流进行中取消 / 断开 / 并发
# ---------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerThread:
    def __init__(self, asgi_app):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(asgi_app, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("uvicorn 未启动")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


def iter_events(resp):
    ev = None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            ev = line[7:]
        elif line.startswith("data: ") and ev:
            yield ev, json.loads(line[6:])
            ev = None


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def live(client):
    with ServerThread(app) as srv:
        with httpx.Client(base_url=srv.url, timeout=10, trust_env=False) as hc:
            yield hc


def _stream(hc, view="coach"):
    return hc.stream("POST", f"/api/v1/athletes/{ATHLETE}/guidance/stream",
                     json={"granularity": "weekly", "window_key": WEEK, "view": view})


class TestLiveCancel:
    def test_cancel_while_writing_stops_upstream_and_releases_lock(self, live, monkeypatch):
        mock = use_mock(monkeypatch, demo_mock(hang_after=3))
        got = []
        with _stream(live) as r:
            assert r.status_code == 200
            it = iter_events(r)
            for ev, d in it:
                got.append(ev)
                if ev == "accepted":
                    task_id = d["task_id"]
                if got.count("delta") == 3:
                    break
            st = live.get("/api/v1/llm/status").json()
            assert st["busy"] and st["state"] == "running" and st["stage"] == "writing"
            c = live.post(f"/api/v1/llm/tasks/{task_id}/cancel")
            assert c.status_code == 202 and c.json()["state"] == "cancelling"
            rest = list(it)
        assert rest[-1][0] == "cancelled" and rest[-1][1]["reason"] == "user"
        assert mock.stream_closed is True
        assert live.get("/api/v1/llm/status").json()["busy"] is False
        # 取消后可立即重新生成（不撞 409）
        use_mock(monkeypatch, demo_mock())
        with _stream(live) as r2:
            assert r2.status_code == 200
            assert list(iter_events(r2))[-1][0] == "final"

    def test_cancel_while_reading(self, live, monkeypatch):
        mock = use_mock(monkeypatch, demo_mock(hang_after=0))
        with _stream(live, "athlete") as r:
            it = iter_events(r)
            for ev, d in it:
                if ev == "accepted":
                    task_id = d["task_id"]
                if ev == "stage" and d["stage"] == "reading":
                    break
            c = live.post(f"/api/v1/llm/tasks/{task_id}/cancel", params={"wait_sec": 3})
            assert c.status_code == 200 and c.json()["state"] == "cancelled"
            rest = list(it)
        assert [e for e, _ in rest] == ["cancelled"]
        assert mock.stream_closed is True

    def test_client_disconnect_cancels_and_releases(self, live, monkeypatch):
        mock = use_mock(monkeypatch, demo_mock(hang_after=2))
        with _stream(live) as r:
            n = 0
            for ev, _ in iter_events(r):
                n += ev == "delta"
                if n == 2:
                    break
        # 退出 with = 客户端断开
        assert wait_until(lambda: live.get("/api/v1/llm/status").json()["busy"] is False)
        assert mock.stream_closed is True

    def test_concurrent_requests_get_409(self, live, monkeypatch):
        use_mock(monkeypatch, demo_mock(hang_after=1))
        with _stream(live) as r:
            it = iter_events(r)
            for ev, d in it:
                if ev == "accepted":
                    task_id = d["task_id"]
                if ev == "delta":
                    break
            r2 = live.post(f"/api/v1/athletes/{ATHLETE}/guidance/stream",
                           json={"granularity": "weekly", "window_key": WEEK, "view": "athlete"})
            assert r2.status_code == 409 and r2.json()["task_id"] == task_id
            r3 = live.post(f"/api/v1/athletes/{ATHLETE}/ask", json={"question": "怎么样？", "view": "coach"})
            assert r3.status_code == 409
            live.post(f"/api/v1/llm/tasks/{task_id}/cancel")
            assert list(it)[-1][0] == "cancelled"


# ---------------------------------------------------------------------------
# 真 HTTP 上游：假 llama-server（OpenAI 兼容 SSE），验证解析与「关闭连接即停」
# ---------------------------------------------------------------------------

def _fake_llama_app(tokens, *, forever=False, delay=0.02, first_delay=0.0, slots=False):
    """假 llama-server。first_delay 模拟 prefill；slots=True 时提供 /slots，并模拟实测行为：
    prefill 期间断开连接，服务端仍把 prompt 处理完（is_processing 保持到 prefill 结束）才停。"""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    state = {"requests": 0, "disconnected": threading.Event(), "sent": 0, "payload": None,
             "busy_until": 0.0, "generating": False}

    async def slots_ep(request):
        busy = state["generating"] or time.monotonic() < state["busy_until"]
        return JSONResponse([{"id": 0, "is_processing": busy}])

    async def chat(request):
        import asyncio
        state["requests"] += 1
        state["payload"] = await request.json()

        async def gen():
            t0 = time.monotonic()
            state["generating"] = True
            try:
                if first_delay:
                    await asyncio.sleep(first_delay)
                i = 0
                while True:
                    if i < len(tokens):
                        tok = tokens[i]
                    elif forever:
                        tok = "继续"
                    else:
                        break
                    chunk = {"choices": [{"index": 0, "delta": {"content": tok}}]}
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    state["sent"] += 1
                    i += 1
                    await asyncio.sleep(delay)
                yield "data: [DONE]\n\n"
            finally:
                state["generating"] = False
                if state["sent"] == 0 and first_delay:
                    state["busy_until"] = t0 + first_delay  # prefill 不可中断：跑完才停
                if state["sent"] < len(tokens) or forever:
                    state["disconnected"].set()
        return StreamingResponse(gen(), media_type="text/event-stream")

    routes = [Route("/v1/chat/completions", chat, methods=["POST"])]
    if slots:
        routes.append(Route("/slots", slots_ep))
    return Starlette(routes=routes), state


class TestRealHTTPUpstream:
    def test_llamacpp_sse_parsed_to_final(self, llm_cfg):
        text = "一、本次概述\n整体平稳，动作一致。\n五、提升方案\n保持节奏。"
        tokens = [text[i:i + 3] for i in range(0, len(text), 3)]
        fake, state = _fake_llama_app(tokens)
        with ServerThread(fake) as up:
            llm_cfg(provider="llamacpp", endpoint=up.url, model="qwen-test")
            c = TestClient(app)
            _ingest_and_report(c, week=WEEK)
            ev = parse_sse(post_guidance(c, "coach").text)
        assert ev[-1][0] == "final" and ev[-1][1]["text"] == text.strip()
        assert state["payload"]["stream"] is True and state["payload"]["model"] == "qwen-test"

    def test_cancel_closes_upstream_connection(self, llm_cfg):
        fake, state = _fake_llama_app(["整体", "平稳"], forever=True, delay=0.05)
        with ServerThread(fake) as up:
            llm_cfg(provider="llamacpp", endpoint=up.url)
            c = TestClient(app)
            _ingest_and_report(c, week=WEEK)
            with ServerThread(app) as eng, httpx.Client(base_url=eng.url, timeout=10, trust_env=False) as hc:
                with _stream(hc) as r:
                    it = iter_events(r)
                    n = 0
                    for ev, d in it:
                        if ev == "accepted":
                            task_id = d["task_id"]
                        n += ev == "delta"
                        if n == 3:
                            break
                    assert not state["disconnected"].is_set()
                    hc.post(f"/api/v1/llm/tasks/{task_id}/cancel")
                    assert list(it)[-1][0] == "cancelled"
                # 上游（假 llama-server）观察到连接断开 → 生成器被取消
                assert state["disconnected"].wait(3)
                sent_at_cancel = state["sent"]
                time.sleep(0.3)
                assert state["sent"] == sent_at_cancel  # 断开后上游不再产出
                assert hc.get("/api/v1/llm/status").json()["busy"] is False


    def test_cancel_in_prefill_holds_lock_until_model_idle(self, llm_cfg):
        """读取阶段取消：连接立刻关，但模型（按 b11435 实测）要把 prompt 处理完才停 →
        引擎保持 cancelling、新请求 409，直到 /slots 显示空闲才发 cancelled 并放锁。"""
        fake, state = _fake_llama_app(["整体"], first_delay=1.2, slots=True)
        with ServerThread(fake) as up:
            llm_cfg(provider="llamacpp", endpoint=up.url)
            c = TestClient(app)
            _ingest_and_report(c, week=WEEK)
            with ServerThread(app) as eng, httpx.Client(base_url=eng.url, timeout=10, trust_env=False) as hc:
                with _stream(hc) as r:
                    it = iter_events(r)
                    for ev, d in it:
                        if ev == "accepted":
                            task_id = d["task_id"]
                        if ev == "stage" and d["stage"] == "reading":
                            break
                    time.sleep(0.2)
                    t_cancel = time.monotonic()
                    hc.post(f"/api/v1/llm/tasks/{task_id}/cancel")
                    assert state["disconnected"].wait(2)  # 上游连接已断
                    st = hc.get("/api/v1/llm/status").json()
                    assert st["busy"] is True and st["state"] == "cancelling"
                    r409 = hc.post(f"/api/v1/athletes/{ATHLETE}/guidance/stream",
                                   json={"granularity": "weekly", "window_key": WEEK, "view": "coach"})
                    assert r409.status_code == 409 and r409.json()["state"] == "cancelling"
                    rest = list(it)
                    waited = time.monotonic() - t_cancel
                assert rest[-1][0] == "cancelled"
                assert waited >= 0.6  # 等到模拟的 prefill 结束（约 1.0 s）才结束
                assert hc.get("/api/v1/llm/status").json()["busy"] is False


class TestStreamLineParser:
    def test_llamacpp_lines(self):
        from app.llm.client import _parse_stream_line
        assert _parse_stream_line("llamacpp", 'data: {"choices":[{"delta":{"content":"环"}}]}') == ("环", False)
        assert _parse_stream_line("llamacpp", 'data: {"choices":[{"delta":{"role":"assistant"}}]}') == ("", False)
        assert _parse_stream_line("llamacpp", "data: [DONE]") == ("", True)
        assert _parse_stream_line("llamacpp", ": keep-alive") == ("", False)
        assert _parse_stream_line("llamacpp", "") == ("", False)

    def test_llamacpp_error_line(self):
        from app.llm.client import LLMError, _parse_stream_line
        with pytest.raises(LLMError):
            _parse_stream_line("llamacpp", 'data: {"error":{"message":"context too long"}}')

    def test_ollama_lines(self):
        from app.llm.client import _parse_stream_line
        assert _parse_stream_line("ollama", '{"message":{"content":"好"},"done":false}') == ("好", False)
        assert _parse_stream_line("ollama", '{"message":{"content":""},"done":true}') == ("", True)
