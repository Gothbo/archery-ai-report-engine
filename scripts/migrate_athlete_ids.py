# -*- coding: utf-8 -*-
"""一次性迁移：旧版可逆 athlete_id（10^18 + 身份证号 / SHA1 派生）→ HMAC 脱敏 athlete_id，并清空身份证号列。

为什么需要：旧版 athlete_id 对纯数字身份证是 10^18+号码（减掉 10^18 即得明文），
session_id（{athlete_id}_{日期}_{nn}）、基线、备注、报告记忆都带着它。

映射来源（只用"精确"来源，不猜）：
1. athlete_profile.identity_id（旧版建档时存的身份证号）
2. --source-db 指定的 display_sys SQLiteTest.db（读 ScoreInfoNew.IdentityID / HeartRateData.IdentityID /
   WindSpeedDirection.RegisterNum 全部身份号，按旧算法重算旧 ID 做比对；只读打开）
无法精确映射的旧 ID 原样保留并在输出中列出（通常是测试数据；也可清库后用新版重新导入）。

用法（先停服务；默认 dry-run 只打印计划）：
    python scripts/migrate_athlete_ids.py --db facts.db --source-db D:\\...\\SQLiteTest.db
    python scripts/migrate_athlete_ids.py --db facts.db --source-db ... --apply

--apply 时：先整库备份为 facts.db.bak.<时间戳>（含旧明文，核对无误后请安全删除），
单事务内改写各表 athlete_id 及含旧 ID 的文本列（session_id 等），清空 identity_id，
删除报告缓存（可重新生成），最后 VACUUM 清除旧页残留。密钥规则同服务端（ENGINE_ID_SECRET /
config.store.id_secret_path / facts.db 同目录 athlete_id.key），须在服务实际使用的配置下运行。

注意：PR #1（场次分组修复）同样要求清空 shot_fact/session_dim 后重新导入；推荐顺序：
迁移本脚本（保住备注/锚点/报告记忆的归属）→ 再按 PR #1 说明清理事实表并重新导入。
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ingest.identity import legacy_athlete_id, pseudonymize_athlete  # noqa: E402

# 表 → 需要替换旧 ID 的列（athlete_id 列精确替换，其余文本列做子串替换：session_id 前缀等）
_ID_COLUMNS = {
    "shot_fact": ["athlete_id"],
    "session_dim": ["athlete_id"],
    "athlete_profile": ["athlete_id"],
    "baseline_snapshots": ["athlete_id"],
    "memory_notes": ["athlete_id", "actor_id"],
    "report_memories": ["athlete_id"],
}
_TEXT_COLUMNS = {
    "shot_fact": ["session_id"],
    "session_dim": ["session_id"],
    "baseline_snapshots": ["source_session_id"],
    "report_memories": ["report_id", "conclusion", "judge_basis", "evidence"],
}


def _source_identities(path: str) -> set[str]:
    out: set[str] = set()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, col in (("ScoreInfoNew", "IdentityID"), ("HeartRateData", "IdentityID"),
                           ("WindSpeedDirection", "RegisterNum")):
            try:
                for (v,) in conn.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"):
                    out.add(str(v).strip())
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return out


def build_mapping(conn: sqlite3.Connection, source_db: str | None) -> tuple[dict[str, str], list[str]]:
    existing: set[str] = set()
    for table in _ID_COLUMNS:
        for (v,) in conn.execute(f"SELECT DISTINCT athlete_id FROM {table}"):
            existing.add(v)
    identities: set[str] = set()
    for (v,) in conn.execute("SELECT identity_id FROM athlete_profile WHERE identity_id IS NOT NULL"):
        identities.add(str(v).strip())
    if source_db:
        identities |= _source_identities(source_db)
    legacy = {legacy_athlete_id(i): i for i in identities if i}
    mapping = {old: pseudonymize_athlete(legacy[old]) for old in existing if old in legacy}
    new_ids = set(mapping.values())
    unmapped = sorted(a for a in existing if a not in mapping and a not in new_ids)
    return mapping, unmapped


def apply_mapping(conn: sqlite3.Connection, mapping: dict[str, str]) -> None:
    with conn:
        for old, new in mapping.items():
            for table, cols in _ID_COLUMNS.items():
                for col in cols:
                    conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (new, old))
            for table, cols in _TEXT_COLUMNS.items():
                for col in cols:
                    conn.execute(f"UPDATE {table} SET {col}=REPLACE({col}, ?, ?) WHERE instr({col}, ?) > 0",
                                 (old, new, old))
        conn.execute("UPDATE athlete_profile SET identity_id=NULL")
        conn.execute("DELETE FROM report_cache")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="facts.db 路径")
    ap.add_argument("--source-db", help="display_sys SQLiteTest.db（只读，用于精确比对旧 ID）")
    ap.add_argument("--apply", action="store_true", help="执行迁移（默认 dry-run）")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"facts.db 不存在：{db_path}")
        return 1
    conn = sqlite3.connect(str(db_path))
    mapping, unmapped = build_mapping(conn, args.source_db)
    print(f"可精确迁移 {len(mapping)} 个运动员；无法映射 {len(unmapped)} 个（保持原样）")
    for old in unmapped:
        print(f"  未映射：…{old[-4:]}")  # 只打印尾号，避免日志里出现可逆 ID
    if not args.apply:
        print("dry-run：未改动。加 --apply 执行。")
        conn.close()
        return 0
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # WAL 内容并回主库，备份才完整
    conn.close()
    backup = db_path.with_name(f"{db_path.name}.bak.{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(db_path, backup)
    conn = sqlite3.connect(str(db_path))
    apply_mapping(conn, mapping)
    conn.close()
    print(f"完成。备份：{backup}（含旧明文，核对后请安全删除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
