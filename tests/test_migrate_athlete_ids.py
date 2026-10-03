# -*- coding: utf-8 -*-
"""P0 隐私迁移脚本：旧可逆 athlete_id → HMAC 脱敏 ID（合成数据，非真实证件号）。"""
import sqlite3

from app.ingest.align import align_session
from app.ingest.base import SessionRaw, ShotRaw
from app.ingest.identity import legacy_athlete_id, pseudonymize_athlete
from app.store.database import Database

IDENT = "000000200001010011"      # 合成：纯数字
IDENT_SRC = "00000020000303003X"  # 合成：只出现在源库里


def _seed_legacy(db: Database, old: str) -> str:
    sid = f"{old}_20240620_01"
    sh = ShotRaw(athlete_id=old, session_id=sid, shot_seq=1, score=9.0, hit=True,
                 shot_time_utc="2024-06-20T01:00:00.000Z")
    dim, rows = align_session(SessionRaw(session_id=sid, athlete_id=old, shots=[sh]))
    db.upsert_session(dim)
    db.insert_shots(rows)
    db.insert_note({"athlete_id": old, "note_type": "goal", "content": "x", "author_role": "athlete",
                    "actor_id": old, "created_at_utc": "2024-06-20T01:00:00.000Z"})
    return sid


def test_migrate_dry_run_and_apply(engine_env, tmp_path):
    from scripts.migrate_athlete_ids import main
    db_path = engine_env[1]
    db = Database(db_path)
    old_a, old_b = legacy_athlete_id(IDENT), legacy_athlete_id(IDENT_SRC)
    assert old_a == "1" + IDENT  # 旧算法确实可逆
    _seed_legacy(db, old_a)
    _seed_legacy(db, old_b)
    _seed_legacy(db, "1999999999999999999")  # 无来源可映射
    db.conn.execute("INSERT INTO athlete_profile (athlete_id, identity_id, name) VALUES (?,?,?)",
                    (old_a, IDENT, "测试员甲"))
    db.conn.commit()
    db.close()
    src = tmp_path / "src.db"
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE ScoreInfoNew (IdentityID TEXT)")
    c.execute("INSERT INTO ScoreInfoNew VALUES (?)", (IDENT_SRC,))
    c.commit()
    c.close()

    assert main(["--db", db_path, "--source-db", str(src)]) == 0  # dry-run 不改
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM shot_fact WHERE athlete_id=?", (old_a,)).fetchone()[0] == 1
    conn.close()

    assert main(["--db", db_path, "--source-db", str(src), "--apply"]) == 0
    new_a, new_b = pseudonymize_athlete(IDENT), pseudonymize_athlete(IDENT_SRC)
    conn = sqlite3.connect(db_path)
    for table in ("shot_fact", "session_dim", "memory_notes"):
        ids = {r[0] for r in conn.execute(f"SELECT athlete_id FROM {table}")}
        assert ids == {new_a, new_b, "1999999999999999999"}, table
    sids = {r[0] for r in conn.execute("SELECT session_id FROM session_dim")}
    assert f"{new_a}_20240620_01" in sids and f"{new_b}_20240620_01" in sids
    assert {r[0] for r in conn.execute("SELECT actor_id FROM memory_notes")} >= {new_a, new_b}
    assert conn.execute("SELECT COUNT(*) FROM athlete_profile WHERE identity_id IS NOT NULL").fetchone()[0] == 0
    dump = "\n".join(conn.iterdump())
    assert IDENT not in dump and IDENT_SRC not in dump
    conn.close()
    assert list(tmp_path.glob("facts.db.bak.*"))
