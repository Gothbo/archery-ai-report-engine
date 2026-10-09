# -*- coding: utf-8 -*-
"""部署验收·本机链路冒烟：真实模型（llamacpp provider）走引擎 ask 全链路。

用途：在开发机上验证「真实模型接入」代码链路可用（D1：演示 1.5B / 部署 7B 走配置），
并暴露 1.5B 在表达质量上的真实表现（含 G2 数值护栏拦截情况）。
部署机 7B 验收请按《部署验收清单》在部署机上执行。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 本机链路冒烟：走 llamacpp + 1.5B（部署机切 7B 只改配置，不改代码）
os.environ["ENGINE_CONFIG"] = str(
    Path(__file__).resolve().parent.parent / "config.llamacpp.local.json")

from app.api.ask import _messages
from app.config import get_config
from app.llm.client import get_llm_client
from app.llm.guardrails import check_guards
from app.rag.context import assemble_context
from app.store.database import get_database

QUESTIONS = [
    "这周为什么进步了？",
    "这周撒放节奏怎么样？",
    "散布表现如何？",
    "内十率和远弹率怎么样？",
]


def main():
    cfg = get_config()
    db = get_database()
    print(f"配置: provider={cfg.llm.provider} model={cfg.llm.model} enabled={cfg.llm.enabled}")
    print(f"数据库: {db.db_path}\n")

    client = get_llm_client(cfg.llm)
    for q in QUESTIONS:
        ctx = assemble_context(db, "1963169497552654337", q, view="coach")
        raw = client.chat(_messages(ctx, cfg))
        ok, reason = check_guards(raw, ctx, cfg.forbidden_phrases)
        print(f"Q: {q}")
        print(f"  LLM 原文: {raw}")
        print(f"  护栏: {'PASS' if ok else 'FAIL -> ' + reason}")
        print()


if __name__ == "__main__":
    main()
