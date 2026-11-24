# -*- coding: utf-8 -*-
"""08 选择性记忆：对话提炼 + 相关性召回（确定性，不引入向量）。

- 提炼：只保留命中「伤病/目标/器材/习惯」的短句，绝不落聊天全文；
- 敏感（伤病）先落候选，确认后才成为正式记忆；
- 召回：档案级记忆（手工/自述）每次注入；对话细节仅在话题相关时注入；
- 候选（confirmed_at_utc 为空）永不参与召回。
"""
from __future__ import annotations

import logging
import re

from app.memory.notes import add_note
from app.store.database import Database

logger = logging.getLogger("engine.memory.selective")

# 伤病/敏感：先候选，运动员确认后才保存
INJURY_MARKERS = ("伤", "疼", "痛", "拉伤", "扭伤", "骨折", "手术", "康复", "不适", "酸")
GOAL_MARKERS = ("目标", "想达到", "想打进", "计划达到", "希望达到", "想拿", "争取")
EQUIPMENT_MARKERS = ("弓", "箭", "瞄", "弦", "箭台", "配重", "磅", "器材", "护指", "弓片")
HABIT_MARKERS = ("习惯", "偏好", "喜欢", "每周", "每天", "通常", "一般会")

# 泛问句不当作个人记忆（避免把「伤病怎么预防」存成自己的伤病）
QUESTION_WORDS = ("怎么", "如何", "为什么", "预防", "避免", "该不该", "能不能", "要不要")

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")
_TRAILING_QUESTION = re.compile(r"(怎么办|怎么处理|要不要|可以吗|行不行|吗|呢|么)$")
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:（）()「」“”\"'\-—…]+")
_MAX_CONTENT = 120

# 召回时忽略的高频二元组，避免「训练/今天」等泛词误命中
_STOP_BIGRAMS = {
    "训练", "今天", "这周", "本周", "觉得", "怎么", "什么", "是否", "可以",
    "我们", "这个", "那个", "现在", "最近", "还是", "没有", "需要", "应该",
    "已经", "一个", "一下", "问题", "情况", "时候", "自己", "我的", "就是",
}


def extract_memories(db: Database, athlete_id: str, text: str, actor_id: str) -> dict:
    """从一次发言中提炼记忆。返回已保存（普通）与待确认（敏感）两类。"""
    saved: list[dict] = []
    candidates: list[dict] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        content = _clean(sentence)
        if len(content) < 2:
            continue
        note_type = _classify(content)
        if note_type is None:
            continue
        if db.find_active_note(athlete_id, content) is not None:
            continue
        confirmed = note_type != "injury"
        note_id = add_note(
            db, athlete_id, note_type, content, "athlete", actor_id,
            provenance="conversation", confirmed=confirmed, quote_text=content,
        )
        row = db.get_note(note_id, athlete_id)
        if row is None:
            continue
        (saved if confirmed else candidates).append(dict(row))
        logger.info("记忆提炼 id=%d athlete=%s type=%s confirmed=%s",
                    note_id, athlete_id, note_type, confirmed)
    return {"athlete_id": athlete_id, "saved": saved, "candidates": candidates}


def select_notes_for_context(notes: list[dict], question: str) -> list[dict]:
    """档案级记忆始终注入；对话细节仅在问题相关时注入。"""
    selected: list[dict] = []
    for note in notes:
        if is_profile_memory(note):
            selected.append(note)
            continue
        haystack = f"{note.get('content', '')}{note.get('quote_text') or ''}"
        if is_relevant(question, haystack):
            selected.append(note)
    return selected


def is_profile_memory(note: dict) -> bool:
    """档案级 = 手工/自述记忆，以及目标与器材（无论来自对话）。

    伤病等对话细节按话题召回，避免每次提无关旧事；手工/自述的伤病例外。
    """
    if (note.get("provenance") or "manual") != "conversation":
        return True
    if note.get("note_type") == "goal":
        return True
    return any(marker in (note.get("content") or "") for marker in EQUIPMENT_MARKERS)


def is_relevant(question: str, text: str) -> bool:
    """确定性相关性：共享一个非泛用二元组即视为相关。"""
    shared = (_bigrams(question) - _STOP_BIGRAMS) & (_bigrams(text) - _STOP_BIGRAMS)
    return bool(shared)


def _bigrams(text: str) -> set[str]:
    cleaned = _PUNCT.sub("", text or "")
    return {cleaned[i:i + 2] for i in range(len(cleaned) - 1)}


def _clean(sentence: str) -> str:
    text = (sentence or "").strip().strip("。！？!?；;，,")
    text = _TRAILING_QUESTION.sub("", text).strip("，, ")
    if any(word in text for word in QUESTION_WORDS):
        return ""
    return text[:_MAX_CONTENT]


def _classify(text: str) -> str | None:
    if any(marker in text for marker in INJURY_MARKERS):
        return "injury"
    if any(marker in text for marker in GOAL_MARKERS):
        return "goal"
    if any(marker in text for marker in EQUIPMENT_MARKERS):
        return "other"
    if any(marker in text for marker in HABIT_MARKERS):
        return "other"
    return None
