# -*- coding: utf-8 -*-
"""API · 对话 ask（B1-4 + 多轮）。

流程：组装上下文（确定性检索）→ 取本视角活动线程最近若干轮 → 检查 llm.enabled：
- 关：降级模板（P1 链路零改动验证点）
- 开：LLM 解读骨架（可参考本线程先前轮次）→ G1/G2/G3 护栏（失败回退降级模板）
返回：answer + sources 溯源 + context_version + conversation_id + suggested_note。
suggested_note 默认只读展示；需用户确认后才经备注 API 落库（provenance=conversation）。
PR #3：调 LLM 前先拿单飞锁（与流式训练指导共用）；引擎忙 → 409 COACH-BUSY（非流式，不可中途取消）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.llm import busy_response
from app.config import get_config
from app.llm.client import LLMError, get_llm_client
from app.llm.guardrails import check_guards
from app.llm.tasks import LLMBusy, get_registry
from app.rag.context import assemble_context, extract_numbers
from app.reports.generator import SessionNotFound
from app.store.database import get_database

router = APIRouter(prefix="/api/v1", tags=["ask"])

DEGRADED_OFF = "对话增强未开启（llm.enabled=false）。可先在报告区生成报告查看结构化结论。"
DEGRADED_LLM_FAIL = "对话服务暂时不可用（本地模型调用失败），可先查看结构化报告。"
DEGRADED_GUARD = "本次解读未通过一致性校验，已回退为报告原文（对话中数字必须能在报告里找到）。"

# llama-server -c 4096：带上最近 12 条（约 6 轮），再按字符上限裁掉更早的轮次
HISTORY_MESSAGE_LIMIT = 12
HISTORY_CHAR_BUDGET = 2400


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=200)
    granularity: str | None = None
    window_key: str | None = None
    view: str = Field(default="athlete", pattern="^(athlete|coach)$")


@router.get("/athletes/{athlete_id}/conversation")
def get_conversation(athlete_id: str, view: str = Query(default="athlete", pattern="^(athlete|coach)$")):
    """返回该运动员在指定视角下的活动线程，供刷新 / 切换运动员后回放。"""
    db = get_database()
    if db.get_profile(athlete_id) is None:
        raise HTTPException(status_code=400, detail=f"运动员 {athlete_id} 不存在")
    conv = db.get_conversation(athlete_id, view)
    if conv is None:
        return {
            "athlete_id": athlete_id,
            "view": view,
            "conversation_id": None,
            "messages": [],
        }
    messages = [_public_message(m) for m in db.list_conversation_messages(conv["id"])]
    return {
        "athlete_id": athlete_id,
        "view": view,
        "conversation_id": conv["id"],
        "updated_at_utc": conv["updated_at_utc"],
        "messages": messages,
    }


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

    conv = db.get_or_create_conversation(athlete_id, body.view)
    prior = list(db.list_conversation_messages(conv["id"], limit=HISTORY_MESSAGE_LIMIT))
    prior = _trim_history(prior)
    _merge_history_numbers(ctx, prior)

    if not cfg.llm.enabled:
        return _finish(db, conv, ctx, DEGRADED_OFF, degraded=True, reason="llm.enabled=false")

    registry = get_registry()
    try:
        task = registry.try_acquire(kind="ask", view=body.view,
                                    deadline_sec=cfg.llm.timeout_sec * (cfg.llm.max_retries + 1),
                                    tz=cfg.timezone, cancellable=False)
    except LLMBusy as exc:
        return busy_response(exc.task)
    try:
        client = get_llm_client(cfg.llm)
        raw = client.chat(_messages(ctx, cfg, prior))
    except LLMError as exc:
        return _finish(db, conv, ctx, DEGRADED_LLM_FAIL, degraded=True, reason=str(exc))
    finally:
        registry.release(task)

    ok, reason = check_guards(raw, ctx, cfg.forbidden_phrases)
    if not ok:
        return _finish(db, conv, ctx, DEGRADED_GUARD, degraded=True, reason=reason,
                       fallback=ctx["skeleton"])
    return _finish(db, conv, ctx, raw, degraded=False)


def _trim_history(rows: list) -> list:
    """超字符预算时从最早的一轮（user+assistant）开始丢弃。"""
    total = sum(len(r["content"] or "") for r in rows)
    trimmed = list(rows)
    while trimmed and total > HISTORY_CHAR_BUDGET:
        dropped = trimmed.pop(0)
        total -= len(dropped["content"] or "")
        if dropped["role"] == "user" and trimmed and trimmed[0]["role"] == "assistant":
            dropped = trimmed.pop(0)
            total -= len(dropped["content"] or "")
    return trimmed


def _merge_history_numbers(ctx: dict, prior: list) -> None:
    """本线程已出现的数字并入允许集合，避免跟问「刚才那个 10.21」被 G2 误降级。"""
    if not prior:
        return
    text = "\n".join(r["content"] or "" for r in prior)
    extra = extract_numbers(text)
    if not extra:
        return
    seen = set(ctx["numbers"])
    merged = list(ctx["numbers"])
    for n in extra:
        if n not in seen:
            seen.add(n)
            merged.append(n)
    ctx["numbers"] = merged


def _messages(ctx: dict, cfg, prior: list) -> list[dict]:
    messages = [
        {"role": "system", "content": (
            "你是射箭训练报告解读助手。只能基于下面的结论骨架以及本线程先前对话回答，"
            "不得使用骨架与本线程以外的知识。\n"
            "规则：\n"
            "1. 只做解读与表达，不做任何新的数值判定；\n"
            "2. 只能引用骨架或本线程先前对话中已经出现的数字，不得出现允许集合以外的数字"
            "（环数、秒、毫米、百分比、日期）；"
            "不得对数字做加减乘除推导，也不要给出允许集合里没有的数值（即使是你自己算出来的）；\n"
            "3. 遇到「为什么/原因」类问题，只陈述骨架中已列出的变化与证据，不得推测骨架未列出的原因"
            "（如装备、心态、训练量）；骨架未给出归因时，直接说明「报告未给出归因」；\n"
            "4. 中文，口语化但专业，尽量不超过 200 字；用连贯短句作答，不要使用编号列表或 Markdown 标记；\n"
            "5. 涉及伤病/训练调整只引用教练备注，不自行给出医疗建议；\n"
            "6. 骨架没有的信息，说明「报告中未包含该信息」。\n"
            "本线程先前轮次可用于接话，但仍不得引入新的数字。\n"
            f"骨架（JSON）：\nskeleton_json: {ctx['skeleton_json']}"
        )},
    ]
    for row in prior:
        role = row["role"]
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": row["content"]})
    messages.append({"role": "user", "content": ctx["question"]})
    return messages


def _persist_turn(db, conv, ctx: dict, answer: str, *, degraded: bool) -> None:
    common = dict(
        report_id=ctx.get("report_id"),
        granularity=ctx.get("granularity"),
        window_key=ctx.get("window_key"),
    )
    db.append_conversation_message(conv["id"], "user", ctx["question"], **common)
    db.append_conversation_message(
        conv["id"], "assistant", answer,
        degraded=degraded, context_version=ctx.get("version"), **common,
    )


def _finish(db, conv, ctx: dict, answer: str, *, degraded: bool, reason: str | None = None,
            fallback: str | None = None) -> dict:
    _persist_turn(db, conv, ctx, answer, degraded=degraded)
    return _answer(ctx, answer, conversation_id=conv["id"], degraded=degraded,
                   reason=reason, fallback=fallback)


def _answer(ctx: dict, answer: str, *, conversation_id: int, degraded: bool,
            reason: str | None = None, fallback: str | None = None) -> dict:
    resp = {
        "athlete_id": ctx["athlete_id"],
        "question": ctx["question"],
        "answer": answer,
        "sources": ctx["sources"],
        "context_version": ctx["version"],
        "granularity": ctx["granularity"],
        "window_key": ctx["window_key"],
        "conversation_id": conversation_id,
        "degraded": degraded,
    }
    if reason:
        resp["reason"] = reason
    if fallback:
        resp["fallback_skeleton"] = fallback
    if ctx["suggested_note"]:
        resp["suggested_note"] = ctx["suggested_note"]
    return resp


def _public_message(row) -> dict:
    out = {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "created_at_utc": row["created_at_utc"],
    }
    if row["report_id"]:
        out["report_id"] = row["report_id"]
    if row["granularity"]:
        out["granularity"] = row["granularity"]
    if row["window_key"]:
        out["window_key"] = row["window_key"]
    if row["degraded"] is not None:
        out["degraded"] = bool(row["degraded"])
    if row["context_version"]:
        out["context_version"] = row["context_version"]
    return out
