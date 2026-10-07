# -*- coding: utf-8 -*-
"""LLM 任务登记 / 单飞锁 / 流式执行器（PR #3：流式 AI 训练指导 + 服务端真取消）。

- 进程内单例 LLMTaskRegistry：同一时间只允许 1 个 LLM 任务（流式指导、非流式 /ask 共用）。
  拿不到锁 → 调用方在打开流之前返回 409 COACH-BUSY（不排队）。
  状态 running → cancelling → 释放；cancelling 期间新请求仍 409，避免「引擎以为空闲、模型其实还在算」。
- 锁只在上游生成确实停止（上游 HTTP 连接已关闭、生成器已退出）之后才释放。
- 取消：显式 cancel 接口为主，客户端断开为兜底；两者都走 request_cancel() → 取消执行器 asyncio 任务，
  CancelledError 打断正在等待的上游读取 → httpx 关闭连接 → llama-server 检测到断开后停止生成。
- 超时：首 token / token 间空闲 / 总截止（llm.first_token_timeout_sec / idle_timeout_sec / total_timeout_sec），
  流式路径不自动重试。
- 前提：单 uvicorn worker（start.bat 默认 1 个）。多 worker 时进程内锁失效。
"""
from __future__ import annotations

import asyncio
import logging
import math
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import AsyncIterator, Awaitable, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger("engine.llm.tasks")

STATE_RUNNING = "running"
STATE_CANCELLING = "cancelling"
STATE_IDLE = "idle"


@dataclass
class LLMTask:
    task_id: str
    kind: str                       # guidance | ask
    view: str
    started_at: datetime
    deadline_at: datetime
    cancellable: bool
    state: str = STATE_RUNNING
    stage: str | None = None        # reading | writing | verifying（ask 为 None）
    cancel_reason: str | None = None  # user | disconnect
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _runner: asyncio.Task | None = field(default=None, repr=False)


class LLMBusy(Exception):
    """单飞锁被占用：携带当前任务信息，供 409 响应。"""

    def __init__(self, task: LLMTask):
        super().__init__("LLM 引擎忙")
        self.task = task


class LLMTaskRegistry:
    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._current: LLMTask | None = None

    # ---- 锁 ----
    def try_acquire(self, *, kind: str, view: str, deadline_sec: float, tz: str,
                    cancellable: bool) -> LLMTask:
        now = datetime.now(ZoneInfo(tz))
        with self._mu:
            if self._current is not None:
                raise LLMBusy(self._current)
            task = LLMTask(task_id=uuid.uuid4().hex[:16], kind=kind, view=view,
                           started_at=now, deadline_at=now + timedelta(seconds=deadline_sec),
                           cancellable=cancellable)
            self._current = task
            return task

    def release(self, task: LLMTask) -> None:
        with self._mu:
            if self._current is task:
                self._current = None

    def current(self) -> LLMTask | None:
        with self._mu:
            return self._current

    def set_stage(self, task: LLMTask, stage: str) -> None:
        with self._mu:
            task.stage = stage

    def mark_stopping(self, task: LLMTask) -> None:
        """非自然结束（取消/超时/超长/上游失败）后等待模型服务空闲期间，对外显示 cancelling。"""
        with self._mu:
            task.state = STATE_CANCELLING

    def bind_runner(self, task: LLMTask, runner: asyncio.Task) -> None:
        with self._mu:
            task._runner = runner
            task._loop = runner.get_loop()
        # 兜底：执行器还没开始运行就被取消时，finally 不会执行 → 由完成回调释放（release 幂等）
        runner.add_done_callback(lambda _t: self.release(task))

    # ---- 取消 ----
    def request_cancel(self, task_id: str, reason: str) -> LLMTask | None:
        """置 cancelling 并取消执行器任务（幂等）。返回任务；任务不存在/已结束返回 None。
        不可取消的任务（非流式 /ask）原样返回、状态不变，由调用方返回 409。"""
        with self._mu:
            task = self._current
            if task is None or task.task_id != task_id:
                return None
            if not task.cancellable or task.state == STATE_CANCELLING:
                return task
            task.state = STATE_CANCELLING
            task.cancel_reason = reason
            runner, loop = task._runner, task._loop
        if runner is not None and loop is not None and not runner.done():
            loop.call_soon_threadsafe(runner.cancel)
        return task

    # ---- 状态 ----
    def status(self) -> dict:
        with self._mu:
            t = self._current
            if t is None:
                return {"busy": False, "state": STATE_IDLE, "task_id": None, "kind": None,
                        "stage": None, "started_at": None, "deadline_at": None}
            return {"busy": True, "state": t.state, "task_id": t.task_id, "kind": t.kind,
                    "stage": t.stage, "started_at": t.started_at.isoformat(timespec="seconds"),
                    "deadline_at": t.deadline_at.isoformat(timespec="seconds")}


def busy_payload(task: LLMTask) -> tuple[dict, int]:
    """409 响应体 + Retry-After 秒数（运行中按截止时间估算；取消中按 2 s）。"""
    if task.state == STATE_CANCELLING:
        retry = 2
    else:
        remaining = (task.deadline_at - datetime.now(task.deadline_at.tzinfo)).total_seconds()
        retry = max(1, math.ceil(remaining))
    return ({"code": "COACH-BUSY", "detail": "引擎正在生成另一份内容，请稍后再试",
             "task_id": task.task_id, "state": task.state, "retry_after_sec": retry}, retry)


_REGISTRY = LLMTaskRegistry()


def get_registry() -> LLMTaskRegistry:
    return _REGISTRY


def reset_registry() -> None:
    """测试用：清空登记表（进程重启等价）。"""
    global _REGISTRY
    _REGISTRY = LLMTaskRegistry()


# ---------------------------------------------------------------------------
# 流式执行器
# ---------------------------------------------------------------------------

class _StageTimeout(Exception):
    def __init__(self, which: str, seconds: float):
        super().__init__(which)
        self.which = which
        self.seconds = seconds


async def _next_with_timeout(agen: AsyncIterator[str], timeout: float) -> str:
    # 3.11+ 用 asyncio.timeout（在当前任务内等待，不跨任务迭代生成器）；3.10 退回 wait_for
    if sys.version_info >= (3, 11):
        async with asyncio.timeout(timeout):
            return await agen.__anext__()
    return await asyncio.wait_for(agen.__anext__(), timeout)  # pragma: no cover


@dataclass
class StreamOutcome:
    """执行器的终止结果：kind ∈ final | guardrail_failed | error | cancelled。"""
    kind: str
    data: dict


async def run_stream_task(
    *,
    registry: LLMTaskRegistry,
    task: LLMTask,
    stream_factory: Callable[[], AsyncIterator[str]],
    emit: Callable[[str, dict], Awaitable[None]],
    finish: Callable[[str], StreamOutcome],
    send_deltas: bool,
    first_token_timeout: float,
    idle_timeout: float,
    total_timeout: float,
    max_len: int,
    wait_upstream_idle: Callable[[float], Awaitable[object]] | None = None,
) -> StreamOutcome:
    """消费上游流 → 通过 emit 发 stage/delta 事件；结束后调用 finish(全文) 做护栏。

    终止事件**不在这里发**：返回 StreamOutcome，由调用方在锁释放之后再发，
    保证客户端收到终止事件时立即重试不会撞 409。
    """
    start = time.monotonic()
    deadline = start + total_timeout
    agen = stream_factory()
    parts: list[str] = []
    length = 0
    outcome: StreamOutcome | None = None
    upstream_done = False  # 上游自然结束（[DONE] / 流关闭）
    try:
        registry.set_stage(task, "reading")
        await emit("stage", {"stage": "reading"})
        first = True
        while True:
            remaining = deadline - time.monotonic()
            stage_limit = first_token_timeout if first else idle_timeout
            if remaining <= 0:
                raise _StageTimeout("total", total_timeout)
            if remaining < stage_limit:
                which, limit = "total", remaining
            else:
                which, limit = ("first_token" if first else "idle"), stage_limit
            try:
                piece = await _next_with_timeout(agen, limit)
            except StopAsyncIteration:
                upstream_done = True
                break
            except TimeoutError:
                raise _StageTimeout(which, total_timeout if which == "total" else stage_limit)
            if not piece:
                continue
            if first:
                first = False
                registry.set_stage(task, "writing")
                await emit("stage", {"stage": "writing"})
            parts.append(piece)
            length += len(piece)
            if send_deltas:
                await emit("delta", {"text": piece})
            if length > max_len:
                # 已超 G3 上限：提前停上游（省算力），整段判护栏不通过
                logger.info("task=%s 输出超长（>%d 字），提前停止上游", task.task_id, max_len)
                break
        registry.set_stage(task, "verifying")
        await emit("stage", {"stage": "verifying"})
        outcome = finish("".join(parts))
    except asyncio.CancelledError:
        cur = asyncio.current_task()
        if cur is not None and hasattr(cur, "uncancel"):
            cur.uncancel()  # 取消已被本执行器处理；清掉计数，避免影响 finally 中的收尾
        reason = task.cancel_reason or "shutdown"
        logger.info("task=%s 已取消（%s），正在关闭上游", task.task_id, reason)
        outcome = StreamOutcome("cancelled", {"reason": reason})
    except _StageTimeout as exc:
        names = {"first_token": "等待第一个字", "idle": "两个字之间", "total": "总时长"}
        outcome = StreamOutcome("error", {
            "code": "COACH-TIMEOUT", "timeout": exc.which,
            "message": f"指导生成中断：{names[exc.which]}超过 {exc.seconds:g} 秒，已停止；成绩不受影响。"})
    except Exception as exc:  # noqa: BLE001 —— 上游任何失败都要落成 error 终止事件、释放锁
        from app.llm.client import LLMOfflineError
        code = "COACH-OFFLINE" if isinstance(exc, LLMOfflineError) else "COACH-LLM"
        logger.warning("task=%s 上游失败：%s", task.task_id, exc)
        outcome = StreamOutcome("error", {"code": code, "message": "指导生成中断：本机模型服务出错，已停止；成绩不受影响。",
                                          "detail": str(exc)[:300]})
    finally:
        # 先确保上游真正关闭，再释放锁（cancelling 期间新请求仍 409）
        try:
            await asyncio.shield(_aclose_quietly(agen))
        except BaseException:  # noqa: BLE001
            pass
        if not upstream_done and wait_upstream_idle is not None:
            # 连接关了不等于模型停了（读取阶段 prefill 可能还要跑完）：确认空闲后再放锁
            registry.mark_stopping(task)
            try:
                idle = await asyncio.shield(wait_upstream_idle(first_token_timeout))
                if idle is False:
                    logger.warning("task=%s 等待模型服务空闲超时（%ss），仍释放锁", task.task_id, first_token_timeout)
            except BaseException:  # noqa: BLE001
                pass
        registry.release(task)
    return outcome


async def _aclose_quietly(agen) -> None:
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001
        pass
