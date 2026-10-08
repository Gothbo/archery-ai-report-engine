# -*- coding: utf-8 -*-
"""并发回归（真引擎联调 #1）：FastAPI 同步路由在线程池里并发执行，多个线程共用 Database 的
同一个 sqlite3 连接（check_same_thread=False）→ 偶发 sqlite3.InterfaceError「bad parameter or
other API misuse」、Row 取值 IndexError，甚至读到别的线程的结果行。

修复：Database 内部一把可重入锁串行化连接使用；v1.2 接收端整批持锁（事务不被其他线程插入的
commit / 读打断）。这里用 16 线程压测：修复前稳定复现，修复后必须零错误、结果逐条正确。
"""
from __future__ import annotations

import concurrent.futures as cf

from fastapi.testclient import TestClient

from tests.conftest import ATHLETE

N_ATHLETES = 5
WORKERS = 16


def _seed(db) -> None:
    for i in range(N_ATHLETES):
        db.upsert_profile({"athlete_id": f"A{i}", "name": f"n{i}"})
        for j in range(i + 1):  # 运动员 Ai 有 i+1 场 / i+1 份报告 → 结果行数可校验
            db.upsert_session({"session_id": f"A{i}-S{j}", "athlete_id": f"A{i}",
                               "session_time_utc": f"2026-08-0{j + 1}T00:00:00.000Z", "distance_m": 70,
                               "shot_count": 0, "mode_composition": "1"})
            db.put_cached_report(f"A{i}-R{j}", f"A{i}", "daily", f"daily:A{i}-S{j}", None, None)


def _read(db, k: int) -> str | None:
    i = k % N_ATHLETES
    try:
        if k % 2:
            rows = [dict(r) for r in db.sessions_of_athlete(f"A{i}")]
        else:
            rows = [dict(r) for r in db.query(
                "SELECT report_id, athlete_id, granularity, window_key FROM report_cache "
                "WHERE athlete_id=? ORDER BY generated_at_utc DESC", (f"A{i}",))]
        if len(rows) != i + 1 or any(r["athlete_id"] != f"A{i}" for r in rows):
            return f"wrong rows for A{i}: {len(rows)}"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def test_shared_database_concurrent_reads(db):
    _seed(db)
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        errors = [e for e in ex.map(lambda k: _read(db, k), range(4000)) if e]
    assert errors == [], f"{len(errors)} 次失败，例：{errors[:3]}"


def test_concurrent_reads_with_note_writes_and_v12_ingest(db):
    """读 + 备注写 + v1.2 接收端（显式事务）同时进行：无异常、写入不丢、接收端整批受理。"""
    from app.ingest.v12.receiver import ingest_messages
    from tests.test_v12_ingest import make_dt2
    _seed(db)

    def write_note(k: int) -> str | None:
        try:
            db.insert_note({"athlete_id": "A0", "note_type": "goal", "content": f"c{k}",
                            "author_role": "coach", "actor_id": "coach", "created_at_utc": "2026-10-08T00:00:00.000Z"})
            return None
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    def ingest(k: int) -> str | None:
        try:
            out = ingest_messages(db, [make_dt2(n, mid_prefix=f"c{k}", shot_base=1963169497552700000 + k * 100)
                                       for n in range(5)])
            return None if out["summary"]["accepted"] == 5 else f"ingest summary {out['summary']}"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

    jobs = []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for k in range(3000):
            if k % 50 == 0:
                jobs.append(ex.submit(ingest, k))
            elif k % 10 == 0:
                jobs.append(ex.submit(write_note, k))
            else:
                jobs.append(ex.submit(_read, db, k))
        errors = [e for e in (j.result() for j in jobs) if e]
    assert errors == [], f"{len(errors)} 次失败，例：{errors[:3]}"
    assert len(db.list_notes("A0")) == len(range(0, 3000, 10)) - len(range(0, 3000, 50))
    assert db.query("SELECT COUNT(*) AS n FROM v12_shot")[0]["n"] == 5 * len(range(0, 3000, 50))


def test_api_concurrent_sessions_and_reports(engine_env):
    """联调复现原样：16 线程并发 GET /athletes/{id}/sessions 与 /athletes/{id}/reports。"""
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as client:  # 500 计入失败而不是直接抛出
        assert client.post("/api/v1/ingest/mock").status_code == 200
        sessions = client.get(f"/api/v1/athletes/{ATHLETE}/sessions").json()["sessions"]
        for s in sessions[:3]:
            assert client.post(f"/api/v1/athletes/{ATHLETE}/reports/daily",
                               params={"session_id": s["session_id"]}).status_code == 200
        paths = [f"/api/v1/athletes/{ATHLETE}/{p}" for p in ("sessions", "reports")] * 300

        def get(path: str) -> str | None:
            r = client.get(path)
            if r.status_code != 200:
                return f"{path} → {r.status_code}"
            body = r.json()
            n = len(body["sessions"]) if path.endswith("sessions") else len(body["reports"])
            want = len(sessions) if path.endswith("sessions") else 3
            return None if n == want else f"{path} → {n} 行（应为 {want}）"

        with cf.ThreadPoolExecutor(WORKERS) as ex:
            errors = [e for e in ex.map(get, paths) if e]
    assert errors == [], f"{len(errors)} 次失败，例：{errors[:3]}"
