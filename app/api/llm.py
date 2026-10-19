# -*- coding: utf-8 -*-
"""API · LLM 任务状态与取消（PR #3）。

- GET  /api/v1/llm/status                 当前是否有 LLM 任务在跑（不含运动员身份与内容）
- POST /api/v1/llm/tasks/{task_id}/cancel 显式取消（真正停止上游生成；锁在上游停止后才释放）
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.config import get_config
from app.llm.tasks import LLMTask, busy_payload, get_registry

router = APIRouter(prefix="/api/v1", tags=["llm"])


def busy_response(task: LLMTask) -> JSONResponse:
    """409 COACH-BUSY：在打开流之前以普通 JSON 返回，带 Retry-After 头。"""
    payload, retry = busy_payload(task)
    return JSONResponse(status_code=409, content=payload, headers={"Retry-After": str(retry)})


@router.get("/llm/status")
def llm_status() -> dict:
    body = get_registry().status()
    body["llm_enabled"] = get_config().llm.enabled
    return body


@router.post("/llm/tasks/{task_id}/cancel")
async def cancel_task(task_id: str,
                      wait_sec: float = Query(default=0.0, ge=0.0, le=10.0)) -> JSONResponse:
    """取消任务。默认立即返回 202 cancelling；任务真正停止时原 SSE 流发 cancelled 事件。
    wait_sec>0：最多等这么久，期间上游停止并释放锁则返回 200 cancelled（给不读 SSE 的宿主用）。"""
    registry = get_registry()
    task = registry.request_cancel(task_id, "user")
    if task is None:
        return JSONResponse(status_code=404, content={
            "code": "COACH-TASK-NOT-FOUND", "detail": "任务不存在或已经结束", "task_id": task_id})
    if not task.cancellable:
        payload, retry = busy_payload(task)
        payload.update(code="COACH-NOT-CANCELLABLE",
                       detail="非流式问答任务无法中途取消，请等它结束（见 Retry-After）")
        return JSONResponse(status_code=409, content=payload, headers={"Retry-After": str(retry)})
    end = time.monotonic() + wait_sec
    while wait_sec and time.monotonic() < end:
        if registry.current() is not task:
            return JSONResponse(status_code=200, content={"task_id": task_id, "state": "cancelled"})
        await asyncio.sleep(0.05)
    if wait_sec and registry.current() is not task:
        return JSONResponse(status_code=200, content={"task_id": task_id, "state": "cancelled"})
    return JSONResponse(status_code=202, content={"task_id": task_id, "state": "cancelling"})
