# -*- coding: utf-8 -*-
"""接入层：Source → align → 事实库，幂等导入。

- 幂等：session_dim upsert 覆盖；shot_fact UNIQUE(session_id, shot_seq) 冲突跳过
- 导入成功回调 on_imported：触发记忆层基线重算（B7：记忆层异常仅记日志，不阻断导入）
- 返回统计：{sessions, inserted_shots, skipped_shots}
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.ingest.align import align_session
from app.ingest.base import Source
from app.store.database import Database

logger = logging.getLogger("engine.ingest")


def ingest_source(
    db: Database,
    source: Source,
    on_imported: Callable[[Database, str, list[str]], None] | None = None,
) -> dict[str, Any]:
    stats = {"sessions": 0, "inserted_shots": 0, "skipped_shots": 0}
    imported_session_ids: list[str] = []
    athlete_ids: set[str] = set()
    for session in source.iter_sessions():
        dim, fact_rows = align_session(session)
        db.upsert_session(dim)
        inserted = db.insert_shots(fact_rows)
        stats["sessions"] += 1
        stats["inserted_shots"] += inserted
        stats["skipped_shots"] += len(fact_rows) - inserted
        imported_session_ids.append(session.session_id)
        athlete_ids.add(session.athlete_id)
        logger.info("导入场次 %s 箭%d 写入%d 跳过%d", session.session_id, len(fact_rows), inserted, len(fact_rows) - inserted)

    # 档案建档：数据源自带档案（姓名等）→ 幂等 upsert（B5；SQLite 源由 /ingest/sqlite 显式建档的旧逻辑已并入此处）
    for aid, p in source.athlete_profiles().items():
        db.upsert_profile({"athlete_id": aid, **p})

    # B10①：同 session_id 重新导入 → 失效该场次缓存报告
    for sid in imported_session_ids:
        for aid in athlete_ids:
            db.invalidate_session_cache(aid, sid)

    if on_imported is not None:
        for aid in athlete_ids:
            try:
                on_imported(db, aid, imported_session_ids)
            except Exception as exc:  # B7：记忆层未就绪只记日志，不阻断导入
                logger.warning("导入后记忆更新失败（跳过）: %s", exc)
    return stats
