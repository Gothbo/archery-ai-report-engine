# -*- coding: utf-8 -*-
"""事实层 + 记忆层数据库访问（SQLite，WAL / Row factory / 幂等 / 单例降级）。

借鉴参考项目 store 经验：WAL、跨线程连接、Row factory、单例缓存、初始化失败返回降级。
M2：shot_fact/session_dim 读写；M2b：记忆表读写方法随后并入（profile/baseline/notes/memories）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from app.config import EngineConfig, get_config
from app.store.schema import SCHEMA_DDL

logger = logging.getLogger("engine.store")

_SINGLETON: "Database | None" = None


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Database:
    """SQLite 访问封装。单连接（check_same_thread=False），WAL 模式。

    FastAPI 同步路由在线程池里并发执行：同一连接被多线程同时使用会偶发
    InterfaceError / 结果行错乱（真引擎联调 #1）。所以连接的每次使用都在 self.lock
    （可重入锁）内串行；直接用 .conn 的调用方（v1.2 接收端）必须先持有 db.lock。
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_DDL)
        self._migrate_columns()
        self._migrate_portal_p0()
        self._conn.commit()

    # ---- 连接管理 ----

    def close(self) -> None:
        with self.lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @property
    def conn(self) -> sqlite3.Connection:
        """原始连接：调用方必须在 `with db.lock:` 内使用（含事务全程）。"""
        return self._conn

    # ---- 列级迁移（CREATE TABLE IF NOT EXISTS 不补列；对已存在库幂等补列）----

    _ADD_COLUMNS: dict[str, list[tuple[str, str]]] = {
        "shot_fact": [
            ("shot_id", "TEXT"),
            ("score_id", "TEXT"),
            ("lane", "TEXT"),
            ("release_time_utc", "TEXT"),
            ("hit_time_utc", "TEXT"),
            ("flight_time_ms", "INTEGER"),
            ("inner_ten", "INTEGER"),
        ],
        "memory_notes": [
            ("provenance", "TEXT"),
            ("quote_text", "TEXT"),
            ("confirmed_at_utc", "TEXT"),
        ],
        # PR #4：引擎本地展示编号（顺序号，与身份 / HMAC 无数学关系；重建库会重新编号）
        "athlete_profile": [
            ("display_no", "INTEGER"),
        ],
        # PR #4：报告正文落库（不含「近期备注」一节；读取时按视角现取备注、运动员视角剔除 coach_extra）
        "report_cache": [
            ("report_json", "TEXT"),
        ],
    }

    def _migrate_columns(self) -> None:
        for table, cols in self._ADD_COLUMNS.items():
            existing = {row["name"] for row in self._query(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in existing:
                    self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_portal_p0(self) -> None:
        """PR #4 数据迁移（幂等，每次启动执行）：
        1. 给没有 display_no 的档案按插入顺序（rowid）依次编号，建唯一索引；
        2. 清空旧版 v1.2 自动生成的名字「运动员{athlete_id 后四位}」（由 HMAC 派生，隐私问题）：
           只清恰好等于该模式的名字，手工改过的名字不动 → 门户显示「未命名选手」。
        """
        self._assign_display_no()
        self._execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_display_no ON athlete_profile(display_no)")
        cur = self._execute(
            "UPDATE athlete_profile SET name=NULL WHERE name = '运动员' || substr(athlete_id, -4)")
        if cur.rowcount:
            logger.info("迁移：清空 %d 个由脱敏 ID 派生的自动名字", cur.rowcount)

    def _assign_display_no(self, athlete_id: str | None = None) -> None:
        sql = "SELECT rowid AS rid FROM athlete_profile WHERE display_no IS NULL"
        params: tuple = ()
        if athlete_id is not None:
            sql += " AND athlete_id=?"
            params = (athlete_id,)
        with self.lock:  # 取 MAX + 逐行编号须整体串行，否则并发建档会撞唯一索引
            rows = self._query(sql + " ORDER BY rowid", params)
            if not rows:
                return
            start = self._query("SELECT COALESCE(MAX(display_no), 0) AS m FROM athlete_profile")[0]["m"]
            for i, r in enumerate(rows, 1):
                self._conn.execute("UPDATE athlete_profile SET display_no=? WHERE rowid=?", (start + i, r["rid"]))
            self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self._conn.execute(sql, params).fetchall()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """公共只读查询（报告/记忆层使用）。"""
        return self._query(sql, params)

    # ---- M2：事实层 ----

    def upsert_session(self, dim: dict) -> None:
        """session_dim upsert（幂等：同 session_id 覆盖）。"""
        self._execute(
            """INSERT INTO session_dim
                 (session_id, athlete_id, session_time_utc, distance_m, shot_count,
                  mode_composition, avg_wind, wind_stddev, site)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(session_id) DO UPDATE SET
                 athlete_id=excluded.athlete_id, session_time_utc=excluded.session_time_utc,
                 distance_m=excluded.distance_m, shot_count=excluded.shot_count,
                 mode_composition=excluded.mode_composition, avg_wind=excluded.avg_wind,
                 wind_stddev=excluded.wind_stddev, site=excluded.site""",
            (
                dim["session_id"],
                dim["athlete_id"],
                dim["session_time_utc"],
                dim["distance_m"],
                dim["shot_count"],
                dim["mode_composition"],
                dim.get("avg_wind"),
                dim.get("wind_stddev"),
                dim.get("site"),
            ),
        )

    def insert_shots(self, rows: list[dict]) -> int:
        """批量写 shot_fact，UNIQUE(session_id, shot_seq) 冲突跳过（幂等）。返回实际写入数。"""
        if not rows:
            return 0
        data = [
            (
                r["athlete_id"],
                r["session_id"],
                r["shot_seq"],
                r["score"],
                int(r["hit"]),
                r["x_mm"],
                r["y_mm"],
                r["mcr_t"],
                r["hr"],
                r["wind_speed"],
                r["wind_dir_deg"],
                r["shooting_mode"],
                r["bow_type"],
                r["video_ref"],
                r["shot_time_utc"],
                r.get("shot_id"),
                r.get("score_id"),
                r.get("lane"),
                r.get("release_time_utc"),
                r.get("hit_time_utc"),
                r.get("flight_time_ms"),
                r.get("inner_ten"),
            )
            for r in rows
        ]
        with self.lock:
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO shot_fact
                     (athlete_id, session_id, shot_seq, score, hit, x_mm, y_mm, mcr_t, hr,
                      wind_speed, wind_dir_deg, shooting_mode, bow_type, video_ref, shot_time_utc,
                      shot_id, score_id, lane, release_time_utc, hit_time_utc, flight_time_ms, inner_ten)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                data,
            )
            self._conn.commit()
            return cur.rowcount

    def session_exists(self, session_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM session_dim WHERE session_id=?", (session_id,)))

    def shots_of_session(self, session_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM shot_fact WHERE session_id=? ORDER BY shot_seq", (session_id,)
        )

    def shots_of_athlete(self, athlete_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM shot_fact WHERE athlete_id=? ORDER BY shot_time_utc", (athlete_id,)
        )

    def sessions_of_athlete(self, athlete_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT * FROM session_dim WHERE athlete_id=? ORDER BY session_time_utc", (athlete_id,)
        )

    # ---- M2b：记忆层 ----

    def upsert_profile(self, profile: dict) -> None:
        """档案 upsert。P0 隐私：identity_id（身份证号）一律不落库，传入也忽略（恒写 NULL）。"""
        self._execute(
            """INSERT INTO athlete_profile
                 (athlete_id, account_id, identity_id, name, gender, age, bow_type, hand, level, updated_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(athlete_id) DO UPDATE SET
                 account_id=excluded.account_id, identity_id=excluded.identity_id,
                 name=excluded.name, gender=excluded.gender, age=excluded.age,
                 bow_type=excluded.bow_type, hand=excluded.hand, level=excluded.level,
                 updated_at_utc=excluded.updated_at_utc""",
            (
                profile["athlete_id"],
                profile.get("account_id"),
                None,  # identity_id 已弃用：不存身份证号
                profile.get("name"),
                profile.get("gender"),
                profile.get("age"),
                profile.get("bow_type"),
                profile.get("hand"),
                profile.get("level"),
                _iso_now(),
            ),
        )
        # PR #4：新档案分配展示编号（已有编号的不变；PUT profile 不能改编号）
        self._assign_display_no(profile["athlete_id"])

    def latest_lane(self, athlete_id: str) -> tuple[str | None, str | None]:
        """最近一支带靶位的箭：(lane 原样字符串, shot_time_utc)。没有 → (None, None)。"""
        rows = self._query(
            """SELECT lane, shot_time_utc FROM shot_fact
               WHERE athlete_id=? AND lane IS NOT NULL AND TRIM(lane)<>''
               ORDER BY shot_time_utc DESC LIMIT 1""", (athlete_id,))
        return (rows[0]["lane"], rows[0]["shot_time_utc"]) if rows else (None, None)

    def get_profile(self, athlete_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM athlete_profile WHERE athlete_id=?", (athlete_id,))
        return rows[0] if rows else None

    def insert_baseline_snapshot(self, snap: dict) -> int:
        cur = self._execute(
            """INSERT INTO baseline_snapshots
                 (athlete_id, bow_type, snap_type, n_shots, avg_score, mcr_t, hr_volatility,
                  dispersion_mm, source_session_id, distance_m, mode_composition, avg_wind, collected_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snap["athlete_id"],
                snap["bow_type"],
                snap["snap_type"],
                snap["n_shots"],
                snap.get("avg_score"),
                snap.get("mcr_t"),
                snap.get("hr_volatility"),
                snap.get("dispersion_mm"),
                snap.get("source_session_id"),
                snap.get("distance_m"),
                snap.get("mode_composition"),
                snap.get("avg_wind"),
                snap["collected_at_utc"],
            ),
        )
        return cur.lastrowid

    def latest_snapshot(self, athlete_id: str, snap_type: str, bow_type: str | None = None) -> sqlite3.Row | None:
        sql = "SELECT * FROM baseline_snapshots WHERE athlete_id=? AND snap_type=?"
        params: list = [athlete_id, snap_type]
        if bow_type:
            sql += " AND bow_type=?"
            params.append(bow_type)
        sql += " ORDER BY collected_at_utc DESC LIMIT 1"
        rows = self._query(sql, tuple(params))
        return rows[0] if rows else None

    def list_snapshots(self, athlete_id: str, snap_type: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM baseline_snapshots WHERE athlete_id=?"
        params: list = [athlete_id]
        if snap_type:
            sql += " AND snap_type=?"
            params.append(snap_type)
        sql += " ORDER BY collected_at_utc DESC"
        return self._query(sql, tuple(params))

    def prune_rolling_snapshots(self, athlete_id: str, keep: int) -> None:
        """保留策略（B8）：仅保留最近 N 条滚动快照，更旧自动删除（锚点永不清除）。"""
        self._execute(
            """DELETE FROM baseline_snapshots WHERE snap_type='rolling' AND athlete_id=?
                 AND id NOT IN (
                   SELECT id FROM baseline_snapshots
                   WHERE snap_type='rolling' AND athlete_id=?
                   ORDER BY collected_at_utc DESC LIMIT ?)""",
            (athlete_id, athlete_id, keep),
        )

    def insert_note(self, note: dict) -> int:
        cur = self._execute(
            """INSERT INTO memory_notes
                 (athlete_id, note_type, content, author_role, actor_id, status, created_at_utc, closed_at_utc)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                note["athlete_id"],
                note["note_type"],
                note["content"],
                note["author_role"],
                note["actor_id"],
                "active",
                note["created_at_utc"],
                None,
            ),
        )
        return cur.lastrowid

    def close_note(self, note_id: int, athlete_id: str) -> bool:
        """软删（B1）：DELETE → status='closed'，保留审计；归属校验 WHERE athlete_id=?。"""
        cur = self._execute(
            "UPDATE memory_notes SET status='closed', closed_at_utc=? WHERE id=? AND athlete_id=? AND status='active'",
            (_iso_now(), note_id, athlete_id),
        )
        return cur.rowcount > 0

    def list_notes(self, athlete_id: str, status: str = "active", days: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM memory_notes WHERE athlete_id=? AND status=?"
        params: list = [athlete_id, status]
        if days is not None:
            sql += " AND created_at_utc >= datetime('now','-' || ? || ' days')"
            params.append(str(days))
        sql += " ORDER BY created_at_utc DESC"
        return self._query(sql, tuple(params))

    def insert_report_memory(self, mem: dict) -> int:
        cur = self._execute(
            """INSERT INTO report_memories
                 (athlete_id, report_id, granularity, conclusion_type, conclusion_key,
                  conclusion, judge_basis, delta_value, evidence, generated_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                mem["athlete_id"],
                mem["report_id"],
                mem["granularity"],
                mem["conclusion_type"],
                mem["conclusion_key"],
                mem["conclusion"],
                mem.get("judge_basis"),
                mem.get("delta_value"),
                mem.get("evidence"),
                mem["generated_at_utc"],
            ),
        )
        return cur.lastrowid

    def latest_memory(self, athlete_id: str, granularity: str) -> sqlite3.Row | None:
        """历史结论引用（B2/D5）：同粒度、时间最近 1 条 judgement。"""
        rows = self._query(
            """SELECT * FROM report_memories WHERE athlete_id=? AND granularity=?
                 AND conclusion_type='judgement' ORDER BY generated_at_utc DESC LIMIT 1""",
            (athlete_id, granularity),
        )
        return rows[0] if rows else None

    def list_memories(self, athlete_id: str) -> list[sqlite3.Row]:
        # window_key 取自报告缓存（report_memories 本身不存窗口键），供前端标注结论属于哪个窗口
        return self._query(
            """SELECT m.*, c.window_key AS window_key
                 FROM report_memories m
                 LEFT JOIN report_cache c ON c.report_id = m.report_id
                WHERE m.athlete_id=? ORDER BY m.generated_at_utc DESC""",
            (athlete_id,),
        )

    # ---- 报告缓存（B10/B12）----

    def get_cached_report(self, athlete_id: str, granularity: str, window_key: str, mdc_version: str | None) -> str | None:
        rows = self._query(
            """SELECT report_id FROM report_cache
               WHERE athlete_id=? AND granularity=? AND window_key=?
                 AND (mdc_version IS ? OR mdc_version=?)""",
            (athlete_id, granularity, window_key, mdc_version, mdc_version),
        )
        return rows[0]["report_id"] if rows else None

    def get_cached_report_row(self, athlete_id: str, granularity: str, window_key: str,
                              mdc_version: str | None) -> sqlite3.Row | None:
        """同 get_cached_report，但返回整行（含 report_json；PR #4 之前生成的旧行 report_json 为 NULL）。"""
        rows = self._query(
            """SELECT * FROM report_cache
               WHERE athlete_id=? AND granularity=? AND window_key=?
                 AND (mdc_version IS ? OR mdc_version=?)""",
            (athlete_id, granularity, window_key, mdc_version, mdc_version),
        )
        return rows[0] if rows else None

    def get_report_row(self, report_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM report_cache WHERE report_id=?", (report_id,))
        return rows[0] if rows else None

    def put_cached_report(self, report_id: str, athlete_id: str, granularity: str, window_key: str,
                          mdc_version: str | None, report_json: str | None = None) -> None:
        self._execute(
            """INSERT OR REPLACE INTO report_cache
                 (report_id, athlete_id, granularity, window_key, mdc_version, generated_at_utc, report_json)
               VALUES (?,?,?,?,?,?,?)""",
            (report_id, athlete_id, granularity, window_key, mdc_version, _iso_now(), report_json),
        )

    def purge_report_window(self, athlete_id: str, granularity: str, window_key: str,
                            mdc_version: str | None) -> None:
        """重生成前清理同窗口旧缓存行及其结论记忆。

        report_id 是 uuid 主键，INSERT OR REPLACE 永远只会追加，同窗口重生成会堆积（周报实测 37 行）：
        前端「引用报告」下拉被重复项撑爆，且「近 3 期 / 近 4 周」判定会读到同一份报告的重复行。
        """
        old_ids = [r["report_id"] for r in self._query(
            """SELECT report_id FROM report_cache
               WHERE athlete_id=? AND granularity=? AND window_key=?
                 AND (mdc_version IS ? OR mdc_version=?)""",
            (athlete_id, granularity, window_key, mdc_version, mdc_version))]
        for rid in old_ids:
            self._execute("DELETE FROM report_memories WHERE report_id=?", (rid,))
        self._execute(
            """DELETE FROM report_cache
               WHERE athlete_id=? AND granularity=? AND window_key=?
                 AND (mdc_version IS ? OR mdc_version=?)""",
            (athlete_id, granularity, window_key, mdc_version, mdc_version))

    def ingest_last_received(self, data_types) -> dict[int, str | None]:
        """v1.2 各 dataType 最后受理时间（走索引 idx_ingest_dt_time，每类一次 MAX 查找）。"""
        out: dict[int, str | None] = {}
        for dt in data_types:
            row = self._query(
                "SELECT MAX(received_at_utc) AS t FROM ingest_messages WHERE data_type=?", (dt,))
            out[dt] = row[0]["t"] if row else None
        return out

    def invalidate_session_cache(self, athlete_id: str, session_id: str) -> None:
        """B10①：同 session_id 重新导入 → 失效该场次缓存报告（daily 窗口键 = daily:<session_id>）。"""
        self._execute(
            "DELETE FROM report_cache WHERE athlete_id=? AND granularity='daily' AND window_key=?",
            (athlete_id, f"daily:{session_id}"),
        )


def get_database(cfg: EngineConfig | None = None) -> Database:
    global _SINGLETON
    if _SINGLETON is None:
        cfg = cfg or get_config()
        try:
            _SINGLETON = Database(cfg.store.db_path)
            logger.info("database 就绪 path=%s", cfg.store.db_path)
        except Exception as exc:  # 初始化失败降级：记录日志返回 None 语义由调用方判断
            logger.error("database 初始化失败：%s", exc)
            raise
    return _SINGLETON
