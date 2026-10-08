# -*- coding: utf-8 -*-
"""API · 对话 ask（B1-4）。

流程：组装上下文（确定性检索）→ 检查 llm.enabled：
- 关：降级模板（P1 链路零改动验证点）
- 开：LLM 解读骨架 → G1/G2/G3 护栏（失败回退降级模板）
返回：answer + sources 溯源 + context_version + suggested_note（只读展示，不落库 D4）。
PR #3：调 LLM 前先拿单飞锁（与流式训练指导共用）；引擎忙 → 409 COACH-BUSY（非流式，不可中途取消）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.llm import busy_response
from app.config import get_config
from app.llm.client import LLMError, get_llm_client
from app.llm.guardrails import check_guards
from app.llm.tasks import LLMBusy, get_registry
from app.rag.context import assemble_context
from app.reports.generator import SessionNotFound
from app.store.database import get_database

router = APIRouter(prefix="/api/v1", tags=["ask"])

DEGRADED_OFF = "对话增强未开启（llm.enabled=false）。可先在报告区生成报告查看结构化结论。"
DEGRADED_LLM_FAIL = "对话服务暂时不可用（本地模型调用失败），可先查看结构化报告。"
DEGRADED_GUARD = "本次解读未通过一致性校验，已回退为报告原文（对话中数字必须能在报告里找到）。"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=200)
    granularity: str | None = None
    window_key: str | None = None
    view: str = Field(default="athlete", pattern="^(athlete|coach)$")


@router.post("/athletes/{athlete_id}/ask")
def ask(athlete_id: str, body: AskRequest):
    cfg = get_config()
    db = get_database()
    try:
        ctx = assemble_context(db, athlete_id, body.question,
                               body.granularity, body.window_key, view=body.view)
    except SessionNotFound as exc:
        from app.api.reports import session_not_found
        return session_not_found(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not cfg.llm.enabled:
        return _answer(ctx, DEGRADED_OFF, degraded=True, reason="llm.enabled=false")

    registry = get_registry()
    try:
        task = registry.try_acquire(kind="ask", view=body.view,
                                    deadline_sec=cfg.llm.timeout_sec * (cfg.llm.max_retries + 1),
                                    tz=cfg.timezone, cancellable=False)
    except LLMBusy as exc:
        return busy_response(exc.task)
    try:
        client = get_llm_client(cfg.llm)
        raw = client.chat(_messages(ctx, cfg))
    except LLMError as exc:
        return _answer(ctx, DEGRADED_LLM_FAIL, degraded=True, reason=str(exc))
    finally:
        registry.release(task)

    ok, reason = check_guards(raw, ctx, cfg.forbidden_phrases)
    if not ok:
        return _answer(ctx, DEGRADED_GUARD, degraded=True, reason=reason, fallback=ctx["skeleton"])
    return _answer(ctx, raw, degraded=False)


def _messages(ctx: dict, cfg) -> list[dict]:
    return [
        {"role": "system", "content": (
            "你是射箭训练报告解读助手。只能基于下面的结论骨架回答运动员的问题，不得使用骨架以外的知识。\n"
            "规则：\n"
            "1. 只做解读与表达，不做任何新的数值判定；\n"
            "2. 只能引用骨架中出现的数字，不得出现骨架外数字（环数、秒、毫米、百分比、日期）；"
            "不得对数字做加减乘除推导，也不要给出骨架里没有的数值（即使是你自己算出来的）；\n"
            "3. 遇到「为什么/原因」类问题，只陈述骨架中已列出的变化与证据，不得推测骨架未列出的原因"
            "（如装备、心态、训练量）；骨架未给出归因时，直接说明「报告未给出归因」；\n"
            "4. 中文，口语化但专业，尽量不超过 200 字；用连贯短句作答，不要使用编号列表或 Markdown 标记；\n"
            "5. 涉及伤病/训练调整只引用教练备注，不自行给出医疗建议；\n"
            "6. 骨架没有的信息，说明「报告中未包含该信息」。\n"
            f"骨架（JSON）：\nskeleton_json: {ctx['skeleton_json']}"
        )},
        {"role": "user", "content": ctx["question"]},
    ]


def _answer(ctx: dict, answer: str, *, degraded: bool, reason: str | None = None,
            fallback: str | None = None) -> dict:
    resp = {
        "athlete_id": ctx["athlete_id"],
        "question": ctx["question"],
        "answer": answer,
        "sources": ctx["sources"],
        "context_version": ctx["version"],
        "granularity": ctx["granularity"],
        "window_key": ctx["window_key"],
        "degraded": degraded,
    }
    if reason:
        resp["reason"] = reason
    if fallback:
        resp["fallback_skeleton"] = fallback
    if ctx["suggested_note"]:
        resp["suggested_note"] = ctx["suggested_note"]
    return resp
