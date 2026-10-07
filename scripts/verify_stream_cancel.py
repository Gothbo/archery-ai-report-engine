# -*- coding: utf-8 -*-
"""目标机实测：llama-server 流式「真取消」（读取阶段 / 撰写阶段）+ 引擎端到端取消。

用法（先起 llama-server，建议带 --slots；引擎部分可选）：
    python scripts/verify_stream_cancel.py --llm http://127.0.0.1:8090
    python scripts/verify_stream_cancel.py --llm http://127.0.0.1:8090 \
        --engine http://127.0.0.1:8000 --athlete <athlete_id> --granularity weekly --window 2026-W40

判定方法：断开连接后轮询 llama-server 的 /slots，看 is_processing 何时变回 false；
读取阶段先测一次「不取消时 prefill 要多久」，再在其 30% 处断开，停止耗时应远小于剩余的 70%。
只用 httpx（引擎已有依赖），不改任何数据。
"""
from __future__ import annotations

import argparse
import json
import time
import uuid

import httpx

PARA = ("射箭训练中，撒放节奏、靠位一致性和散布控制是评估技术稳定性的核心维度。"
        "教练会结合环值、弹着分布与心率变化来判断运动员的状态。")


def _client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(600, connect=5), trust_env=False)


def slots_busy(c: httpx.Client, llm: str) -> bool | None:
    try:
        r = c.get(f"{llm}/slots", timeout=5)
        if r.status_code != 200:
            return None
        return any(s.get("is_processing") for s in r.json())
    except Exception:
        return None


def wait_idle(c: httpx.Client, llm: str, t0: float, limit: float = 120) -> float | None:
    while time.monotonic() - t0 < limit:
        b = slots_busy(c, llm)
        if b is None:
            return None
        if not b:
            return time.monotonic() - t0
        time.sleep(0.05)
    return float("inf")


def long_messages(repeat: int) -> list[dict]:
    nonce = uuid.uuid4().hex  # 防止 prompt 缓存命中
    return [{"role": "system", "content": f"[{nonce}] " + PARA * repeat},
            {"role": "user", "content": "请用一段话总结上面的内容。"}]


def stream_req(c: httpx.Client, llm: str, messages, **extra):
    payload = {"messages": messages, "stream": True, "cache_prompt": False, "temperature": 0.2, **extra}
    return c.stream("POST", f"{llm}/v1/chat/completions", json=payload)


def measure_prefill(c, llm, repeat) -> float:
    t0 = time.monotonic()
    with stream_req(c, llm, long_messages(repeat), max_tokens=1) as r:
        for line in r.iter_lines():
            if line.startswith("data:") and '"content"' in line:
                break
    return time.monotonic() - t0


def cancel_in_prefill(llm: str, c: httpx.Client, repeat: int, after: float) -> dict:
    """用裸 socket 发请求、等 after 秒、直接关连接：llama-server 可能要到第一个 token 才回响应头，
    httpx 的 stream() 会卡在等响应头上，没法在 prefill 中途断开。"""
    import socket
    from urllib.parse import urlparse
    u = urlparse(llm)
    body = json.dumps({"messages": long_messages(repeat), "stream": True, "cache_prompt": False,
                       "max_tokens": 256, "temperature": 0.2}, ensure_ascii=False).encode("utf-8")
    req = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
           f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
           ).encode("ascii") + body
    t_start = time.monotonic()
    sock = socket.create_connection((u.hostname, u.port), timeout=5)
    sock.sendall(req)
    time.sleep(after)
    sock.setblocking(False)
    try:
        early = sock.recv(65536)
    except BlockingIOError:
        early = b""
    got_token = b'"content"' in early
    t_close = time.monotonic()
    sock.close()
    idle = wait_idle(c, llm, t_close)
    return {"closed_at_s": round(t_close - t_start, 2),
            "slot_idle_after_close_s": None if idle is None else round(idle, 2),
            "got_token_before_close": got_token}


def cancel_in_writing(c, llm, tokens_before_cancel: int) -> dict:
    msgs = [{"role": "user", "content": "请写一篇 800 字左右的射箭训练心得。"}]
    n = 0
    with stream_req(c, llm, msgs, max_tokens=2048) as r:
        for line in r.iter_lines():
            if line.startswith("data:") and '"content"' in line:
                n += 1
                if n >= tokens_before_cancel:
                    break
        t_close = time.monotonic()
    idle = wait_idle(c, llm, t_close)
    return {"tokens_read": n, "slot_idle_after_close_s": None if idle is None else round(idle, 2)}


def engine_cancel(c, engine, llm, athlete, granularity, window, view, stage) -> dict:
    url = f"{engine}/api/v1/athletes/{athlete}/guidance/stream"
    body = {"granularity": granularity, "window_key": window, "view": view}
    out: dict = {"stage": stage, "view": view}
    t0 = time.monotonic()
    with c.stream("POST", url, json=body) as r:
        if r.status_code != 200:
            out["http"] = r.status_code
            out["body"] = r.read().decode("utf-8", "replace")[:300]
            return out
        ev, task_id, t_cancel = None, None, None
        deltas = 0
        for line in r.iter_lines():
            if line.startswith("event: "):
                ev = line[7:]
                continue
            if not line.startswith("data: "):
                continue
            d = json.loads(line[6:])
            if ev == "accepted":
                task_id = d["task_id"]
            if t_cancel is None and (
                    (stage == "reading" and ev == "stage" and d.get("stage") == "reading")
                    or (stage == "writing" and (ev == "delta" and (deltas := deltas + 1) >= 5
                                                or (view == "athlete" and ev == "stage" and d.get("stage") == "writing")))):
                t_cancel = time.monotonic()
                cr = c.post(f"{engine}/api/v1/llm/tasks/{task_id}/cancel")
                out["cancel_http"] = cr.status_code
            if ev in ("final", "guardrail_failed", "error", "cancelled"):
                out["terminal"] = ev
                out["terminal_after_cancel_s"] = round(time.monotonic() - t_cancel, 2) if t_cancel else None
                break
    st = c.get(f"{engine}/api/v1/llm/status").json()
    out["engine_busy_after"] = st["busy"]
    out["llm_slot_busy_after"] = slots_busy(c, llm)
    out["total_s"] = round(time.monotonic() - t0, 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", default="http://127.0.0.1:8090")
    ap.add_argument("--repeat", type=int, default=40, help="长 prompt 重复段数（约 60 token/段）")
    ap.add_argument("--engine")
    ap.add_argument("--athlete")
    ap.add_argument("--granularity", default="weekly")
    ap.add_argument("--window")
    args = ap.parse_args()
    llm = args.llm.rstrip("/")
    with _client() as c:
        if slots_busy(c, llm) is None:
            print("提示：/slots 不可用（启动 llama-server 时加 --slots），无法判定停止时间。")
        prefill = measure_prefill(c, llm, args.repeat)
        print(f"[基准] 不取消时首 token 耗时（≈prefill）：{prefill:.2f}s（repeat={args.repeat}）")
        res = cancel_in_prefill(llm, c, args.repeat, after=max(0.3, prefill * 0.3))
        remaining = prefill - res["closed_at_s"]
        idle = res["slot_idle_after_close_s"]
        if res["got_token_before_close"]:
            verdict = "无效：断开前已出第一个字，请加大 --repeat"
        elif idle is None:
            verdict = "无法判定（/slots 不可用）"
        elif idle < remaining * 0.5:
            verdict = "prefill 被中途打断"
        else:
            verdict = "prefill 未被打断（服务端处理完 prompt 才停；引擎会保持 cancelling 直到空闲）"
        print(f"[读取阶段取消] {res}；剩余 prefill 约 {remaining:.1f}s → {verdict}")
        print(f"[撰写阶段取消] {cancel_in_writing(c, llm, 10)}")
        if args.engine and args.athlete and args.window:
            for stage in ("reading", "writing"):
                for view in ("coach", "athlete"):
                    print(f"[引擎端到端] {engine_cancel(c, args.engine.rstrip('/'), llm, args.athlete, args.granularity, args.window, view, stage)}")


if __name__ == "__main__":
    main()
