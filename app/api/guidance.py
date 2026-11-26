# -*- coding: utf-8 -*-
"""API · AI 训练指导（PR #3：流式输出 + 服务端真取消）。

POST /api/v1/athletes/{athlete_id}/guidance/stream
- 请求绑定「一份报告（granularity + window_key）+ 视角（coach/athlete）」。
- 打开流之前：llm 未开启 → 503 COACH-OFFLINE；引擎忙 → 409 COACH-BUSY；参数/报告问题 → 400。
- 流（text/event-stream）事件顺序：
  accepted → stage(reading) → stage(writing，收到第一个字时) → delta*（**仅教练视角**）
  → stage(verifying) → 终止事件之一：final / guardrail_failed / error / cancelled。
  读取阶段等无输出时每 ~10 s 发一行注释心跳 `: ping`。
- 护栏只在完整文本上跑（G1/G2/G3）；不通过 → 整段换降级文案（与 /ask 相同）+ 报告原文。
- 运动员视角：服务端从不发送 delta，只发阶段与最终结果；输入骨架本身已按视角排除教练观察。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import aclosing
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.api.ask import DEGRADED_GUARD
from app.api.llm import busy_response
from app.config import get_config
from app.llm.client import LLMError, get_llm_client
from app.llm.guardrails import check_guards, number_guard
from app.llm.tasks import LLMBusy, StreamOutcome, get_registry, run_stream_task
from app.rag.context import ReportContextNotFound, ReportSnapshotUnavailable, assemble_context, check_context_is_current
from app.reports.generator import SessionNotFound
from app.reports.window import GRANULARITIES
from app.store.database import get_database
from app.timeutil import iso_now_utc

logger = logging.getLogger("engine.api.guidance")

router = APIRouter(prefix="/api/v1", tags=["guidance"])

HEARTBEAT_SEC = 10.0
GUIDANCE_QUESTION = "请撰写本期训练指导"
GUIDANCE_HEADINGS = ("一、本次概述", "二、数据基础", "三、技术表现", "四、状态与负荷", "五、提升方案")
GUIDANCE_MARKER = "【训练指导】"  # mock 应答器据此区分指导与问答
DEGRADED_EMPTY = "指导生成中断：模型返回了空结果；成绩不受影响。"


class GuidanceRequest(BaseModel):
    granularity: str = Field(pattern="^(daily|weekly|monthly|quarterly|yearly)$")
    window_key: str = Field(min_length=1, max_length=128)
    view: str = Field(default="athlete", pattern="^(athlete|coach)$")
    report_id: str | None = None


class InterpretRequest(BaseModel):
    granularity: str
    window_key: str
    view: str = "athlete"
    report_id: str | None = None


def _now_iso(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def guidance_messages(ctx: dict, view: str) -> list[dict]:
    audience = ("面向教练：可以使用专业术语，侧重训练安排与重点观察项。" if view == "coach"
                else "面向运动员本人：用「你」称呼，语气平实、鼓励，不写教练内部安排。")
    headings = "\n".join(GUIDANCE_HEADINGS)
    return [
        {"role": "system", "content": (
            f"你是射箭训练指导撰写助手{GUIDANCE_MARKER}。只能基于下面的结论骨架撰写本期训练指导，不得使用骨架以外的知识。\n"
            "输出格式：纯文本，严格按下面五个小标题依次输出，每个小标题单独占一行，小标题下写一到三句；"
            "不要使用 Markdown 标记、编号列表或表格，不要复述本说明：\n"
            f"{headings}\n"
            "规则：\n"
            "1. 只做解读与表达，不做任何新的数值判定；\n"
            "2. 只能引用骨架中出现的数字，不得出现骨架外数字（环数、秒、毫米、百分比、日期）；"
            "不得对数字做加减乘除推导；\n"
            "3. 骨架没有的数据（如心率、负荷），在对应小标题下直接写「报告未包含该数据」，不要编造；\n"
            "4. 涉及伤病/训练调整只引用备注，不自行给出医疗建议；\n"
            "5. 中文，简洁，每个小标题下只写要点；\n"
            f"6. {audience}\n"
            "仅有类型或引用的备注与历史结论代表原文未提供，不得推测其内容。\n"
            f"骨架（JSON）：\nskeleton_json: {ctx['skeleton_json']}"
        )},
        {"role": "user", "content": GUIDANCE_QUESTION},
    ]


def _interpret_context(athlete_id: str, body: InterpretRequest, report_id: str | None) -> dict:
    db = get_database()
    try:
        return assemble_context(db, athlete_id, GUIDANCE_QUESTION, body.granularity,
                                body.window_key, view=body.view, report_id=report_id, strict_expression=True)
    except (ReportContextNotFound, SessionNotFound) as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, OverflowError) as exc:
        raise HTTPException(400, str(exc)) from exc


async def _interpret_tokens(client, messages):
    try:
        async with aclosing(client.astream_chat(messages)) as tokens:
            async for token in tokens:
                yield token
    except LLMError as exc:
        if isinstance(exc.__cause__, (httpx.TimeoutException, TimeoutError)):
            raise TimeoutError from exc
        raise


async def _discard_event(_event: str, _data: dict) -> None:
    pass


def _split_sections(text: str) -> list[dict]:
    sections: list[dict] = []
    lines: list[str] = []
    for line in text.splitlines():
        heading = line.strip()
        if heading in GUIDANCE_HEADINGS:
            if sections:
                sections[-1]["text"] = "\n".join(lines).strip()
                lines.clear()
            sections.append({"heading": heading, "text": ""})
        elif sections:
            lines.append(line)
        elif heading:
            return []
    if sections:
        sections[-1]["text"] = "\n".join(lines).strip()
    if tuple(section["heading"] for section in sections) != GUIDANCE_HEADINGS:
        return []
    return sections if all(section["text"] for section in sections) else []


def _has_next_step(text: str) -> bool:
    in_next_step = False
    for line in text.splitlines():
        heading, *body = re.split(r"[：:]", line.strip(), maxsplit=1)
        if heading.strip() in GUIDANCE_HEADINGS:
            if in_next_step:
                return False
            in_next_step = heading.strip() == GUIDANCE_HEADINGS[-1]
            line = body[0] if body else ""
        if in_next_step and any(char.isalnum() for char in line):
            return True
    return False


def _interpret_response(ctx: dict, *, reason: str | None = None, text: str = "",
                        model_version: str | None = None, quality_gate: dict | None = None) -> dict:
    guide = ctx["rule_guidance"]
    if reason is not None:
        parts = [guide["summary"], *(bullet["text"] for bullet in guide["bullets"])]
        text = "\n".join(part for part in parts if part.strip()) or "本窗口暂无规则指导"
    response = {
        "source": "rule" if reason is not None else "model",
        "degraded_reason": reason,
        "text": text,
        "sections": [] if reason is not None else _split_sections(text),
        "references": ctx["references"],
        "rule_guidance": guide,
        "rule_pack": ctx["rule_pack"],
        "context_version": ctx["version"],
        "model_version": model_version,
        "generated_at_utc": iso_now_utc(),
    }
    if quality_gate is not None:
        response["quality_gate"] = quality_gate
    return response


@router.post("/athletes/{athlete_id}/guidance/interpret")
async def guidance_interpret(athlete_id: str, body: InterpretRequest):
    if body.granularity not in GRANULARITIES:
        raise HTTPException(400, "未知报告粒度")
    if not body.window_key.strip() or len(body.window_key) > 128 or body.window_key != body.window_key.strip():
        raise HTTPException(400, "报告窗口键无效")
    if body.view not in ("athlete", "coach") or body.report_id == "":
        raise HTTPException(400, "报告视角或报告 ID 无效")
    ctx = await run_in_threadpool(_interpret_context, athlete_id, body, body.report_id)
    cfg = get_config()
    if not cfg.llm.enabled:
        return _interpret_response(ctx, reason="disabled")

    registry = get_registry()
    try:
        task = registry.try_acquire(kind="guidance", view=body.view,
                                    deadline_sec=cfg.llm.total_timeout_sec, tz=cfg.timezone, cancellable=False)
    except LLMBusy:
        return _interpret_response(ctx, reason="busy")

    def finish(raw: str) -> StreamOutcome:
        text = raw.strip()
        if not text:
            return StreamOutcome("error", {"code": "COACH-EMPTY"})
        ok, guard_reason = check_guards(raw, ctx, cfg.forbidden_phrases, max_len=cfg.llm.guidance_max_len)
        missing = []
        if not ctx["references"]:
            missing.append("references")
        if not _has_next_step(text):
            missing.append("next_step")
        if not ok and ((guard_reason or "").startswith("G2") or number_guard(raw, ctx)):
            missing.append("numbers")
        gate = {"passed": not missing, "missing": missing}
        if missing:
            return StreamOutcome("quality_failed", {"quality_gate": gate})
        if not ok:
            return StreamOutcome("guardrail_failed", {"quality_gate": gate})
        return StreamOutcome("final", {"text": text, "quality_gate": gate})

    try:
        client = get_llm_client(cfg.llm)
        messages = guidance_messages(ctx, body.view)
        outcome = await run_stream_task(
            registry=registry, task=task, stream_factory=lambda: _interpret_tokens(client, messages),
            emit=_discard_event, finish=finish, send_deltas=False,
            first_token_timeout=cfg.llm.first_token_timeout_sec, idle_timeout=cfg.llm.idle_timeout_sec,
            total_timeout=cfg.llm.total_timeout_sec, max_len=cfg.llm.guidance_max_len,
            wait_upstream_idle=getattr(client, "wait_idle", None),
        )
    except (httpx.TimeoutException, TimeoutError):
        outcome = StreamOutcome("error", {"code": "COACH-TIMEOUT"})
    except Exception:  # The user can still read the rule guidance when the provider fails.
        logger.exception("task=%s 无法完成模型解读", task.task_id)
        outcome = StreamOutcome("error", {})
    finally:
        registry.release(task)

    current = await run_in_threadpool(_interpret_context, athlete_id, body, ctx["report_id"])
    if current["version"] != ctx["version"]:
        return _interpret_response(current, reason="error")
    gate = outcome.data.get("quality_gate")
    if outcome.kind == "final":
        return _interpret_response(current, text=outcome.data["text"],
                                   model_version=cfg.llm.model_version, quality_gate=gate)
    reason = outcome.kind if outcome.kind in ("quality_failed", "guardrail_failed") else {
        "COACH-TIMEOUT": "timeout", "COACH-EMPTY": "empty",
    }.get(outcome.data.get("code"), "error")
    return _interpret_response(current, reason=reason, quality_gate=gate)


@router.post("/athletes/{athlete_id}/guidance/stream")
async def guidance_stream(athlete_id: str, body: GuidanceRequest):
    cfg = get_config()
    if not cfg.llm.enabled:
        return JSONResponse(status_code=503, content={
            "code": "COACH-OFFLINE", "detail": "AI 训练指导未开启（llm.enabled=false），请先查看规则建议"})

    registry = get_registry()
    try:
        task = registry.try_acquire(kind="guidance", view=body.view,
                                    deadline_sec=cfg.llm.total_timeout_sec, tz=cfg.timezone,
                                    cancellable=True)
    except LLMBusy as exc:
        return busy_response(exc.task)

    try:
        db = get_database()
        ctx = await run_in_threadpool(assemble_context, db, athlete_id, GUIDANCE_QUESTION,
                                      body.granularity, body.window_key, view=body.view, report_id=body.report_id)
        client = get_llm_client(cfg.llm)
    except SessionNotFound as exc:
        registry.release(task)
        return JSONResponse(status_code=404, content={"code": "SESSION-NOT-FOUND", "detail": str(exc)})
    except ReportSnapshotUnavailable as exc:
        registry.release(task)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        registry.release(task)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BaseException:
        registry.release(task)
        raise

    view = body.view
    max_len = cfg.llm.guidance_max_len
    messages = guidance_messages(ctx, view)
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await queue.put((event, data))

    def finish(text: str) -> StreamOutcome:
        text = text.strip()
        if not text:
            return StreamOutcome("error", {"code": "COACH-EMPTY", "message": DEGRADED_EMPTY})
        ok, reason = check_guards(text, ctx, cfg.forbidden_phrases, max_len=max_len)
        common = {"sources": ctx["sources"], "context_version": ctx["version"],
                  "granularity": ctx["granularity"], "window_key": ctx["window_key"], "view": view}
        if rule_pack := ctx.get("rule_pack"):
            common["rule_pack"] = rule_pack
        if not ok:
            data = {"code": "COACH-GUARD", "message": DEGRADED_GUARD,
                    "rule": (reason or "")[:2] if (reason or "").startswith("G") else "G0",
                    "fallback_skeleton": ctx["skeleton"], **common}
            if view == "coach":
                data["reason"] = reason  # 运动员视角不回显原因（可能含未核对的文字/数字）
            return StreamOutcome("guardrail_failed", data)
        return StreamOutcome("final", {"text": text, "degraded": False, "guard": {"passed": True},
                                       "generated_at": _now_iso(cfg.timezone), **common})

    t0 = time.monotonic()

    async def runner() -> None:
        outcome = await run_stream_task(
            registry=registry, task=task,
            stream_factory=lambda: client.astream_chat(messages),
            emit=emit, finish=finish, send_deltas=(view == "coach"),
            first_token_timeout=cfg.llm.first_token_timeout_sec,
            idle_timeout=cfg.llm.idle_timeout_sec,
            total_timeout=cfg.llm.total_timeout_sec,
            max_len=max_len,
            wait_upstream_idle=getattr(client, "wait_idle", None),
        )
        if outcome.kind in ("final", "guardrail_failed"):
            try:
                await run_in_threadpool(check_context_is_current, db, ctx, view)
            except ReportContextNotFound as exc:
                outcome = StreamOutcome("error", {"code": "COACH-CONTEXT-STALE", "message": str(exc)})
        logger.info("guidance task=%s view=%s 结束：%s（%.1fs）", task.task_id, view, outcome.kind,
                    time.monotonic() - t0)
        # 走到这里时上游已关闭、锁已释放 → 客户端收到终止事件即可立即重试
        await queue.put(("__outcome__", outcome))

    runner_task = asyncio.create_task(runner())
    registry.bind_runner(task, runner_task)
    runner_task.add_done_callback(lambda _t: queue.put_nowait(("__done__", None)))

    accepted = {
        "task_id": task.task_id, "kind": "guidance", "view": view,
        "granularity": ctx["granularity"], "window_key": ctx["window_key"],
        "report_id": ctx.get("report_id"), "context_version": ctx["version"],
        "started_at": task.started_at.isoformat(timespec="seconds"),
        "deadline_at": task.deadline_at.isoformat(timespec="seconds"),
        "stream_deltas": view == "coach",
    }

    async def event_stream():
        task_id = task.task_id
        try:
            yield _sse("accepted", accepted)
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), HEARTBEAT_SEC)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if event == "__outcome__":
                    yield _sse(data.kind, {"task_id": task_id, **data.data})
                    return
                if event == "__done__":
                    # 执行器未产出结果就结束（如尚未开始运行即被取消）
                    yield _sse("cancelled", {"task_id": task_id, "reason": task.cancel_reason or "shutdown"})
                    return
                yield _sse(event, {"task_id": task_id, **data})
        finally:
            if not runner_task.done():
                # 兜底：客户端断开（关页面 / 切报告时 abort fetch）→ 取消，执行器负责关上游、释放锁
                registry.request_cancel(task_id, "disconnect")

    async def cancel_if_running() -> None:
        if not runner_task.done():
            registry.request_cancel(task.task_id, "disconnect")

    return StreamingResponse(
        event_stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(cancel_if_running),
    )
