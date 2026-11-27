# -*- coding: utf-8 -*-
"""事实层 + 记忆层数据库访问（SQLite，WAL / Row factory / 幂等 / 单例降级）。

借鉴参考项目 store 经验：WAL、跨线程连接、Row factory、单例缓存、初始化失败返回降级。
M2：shot_fact/session_dim 读写；M2b：记忆表读写方法随后并入（profile/baseline/notes/memories）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app.config import EngineConfig, get_config
from app.store.guidance_feedback import backfill_report_observations, record_report_observation
from app.store.schema import SCHEMA_DDL

if TYPE_CHECKING:
    from app.reports.window import WindowKey

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
        from app.reports.rule_packs import seed_rule_pack

        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._writing = False
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_DDL)
        self._instance_id = self._load_instance_id()
        self._migrate_columns()
        self._migrate_memory_closure()
        self._migrate_portal_p0()
        with self.write_transaction():
            backfill_report_observations(self._conn)
        self._purge_legacy_calendar_reports()
        self._purge_legacy_anchor_reports()
        seed_rule_pack(self._conn)
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

    @property
    def db_instance_id(self) -> str:
        """持久化的库实例标识：首次建库生成，重启沿用；换库后必然不同。"""
        return self._instance_id

    def _load_instance_id(self) -> str:
        row = self._conn.execute("SELECT value FROM store_meta WHERE key='db_instance_id'").fetchone()
        if row and row[0]:
            return row[0]
        value = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO store_meta(key, value) VALUES('db_instance_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (value,))
        self._conn.commit()
        logger.info("生成库实例标识 db_instance_id=%s", value)
        return value

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
            ("rule_pack_version", "TEXT REFERENCES rule_packs(version)"),
        ],
    }

    def _migrate_columns(self) -> None:
        for table, cols in self._ADD_COLUMNS.items():
            existing = {row["name"] for row in self._query(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in existing:
                    self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_memory_closure(self) -> None:
        """08 记忆闭环：旧数据视为已确认手工备注，避免升级后从列表/召回里消失。

        约定 confirmed_at_utc IS NULL 只用于 conversation 来源的候选（待确认）。
        """
        cur = self._execute(
            "UPDATE memory_notes SET confirmed_at_utc = created_at_utc "
            "WHERE confirmed_at_utc IS NULL AND COALESCE(provenance, '') <> 'conversation'")
        if cur.rowcount:
            logger.info("迁移：%d 条旧备注标记为已确认", cur.rowcount)

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
            if not self._writing:
                self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            if self._writing:
                return self._conn.execute(sql, params)
            with self._conn:
                return self._conn.execute(sql, params)

    @contextmanager
    def write_transaction(self):
        """Group store writes under one lock and one SQLite commit."""
        with self.lock:
            if self._writing:
                yield
                return
            self._writing = True
            try:
                with self._conn:
                    if not self._conn.in_transaction:
                        self._conn.execute("BEGIN IMMEDIATE")
                    yield
            finally:
                self._writing = False

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
        with self.write_transaction():
            cur = self._conn.executemany(
                """INSERT OR IGNORE INTO shot_fact
                     (athlete_id, session_id, shot_seq, score, hit, x_mm, y_mm, mcr_t, hr,
                      wind_speed, wind_dir_deg, shooting_mode, bow_type, video_ref, shot_time_utc,
                      shot_id, score_id, lane, release_time_utc, hit_time_utc, flight_time_ms, inner_ten)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                data,
            )
            return cur.rowcount

    def replace_desktop_session(self, dim: dict, rows: list[dict]) -> None:
        with self.write_transaction():
            athlete_id, session_id = dim["athlete_id"], dim["session_id"]
            active = not self.is_session_excluded(athlete_id, session_id)
            if active and self.session_exists(session_id):
                self._invalidate_session_reports(athlete_id, session_id)
            self._conn.execute("DELETE FROM shot_fact WHERE session_id=?", (session_id,))
            self.upsert_session(dim)
            self.insert_shots(rows)
            if active:
                self._invalidate_session_reports(athlete_id, session_id)

    def session_exists(self, session_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM session_dim WHERE session_id=?", (session_id,)))

    def shots_of_session(self, session_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT f.* FROM shot_fact f WHERE f.session_id=? AND NOT EXISTS "
            "(SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id AND l.session_id=f.session_id "
            "AND l.excluded_at_utc IS NOT NULL) ORDER BY f.shot_seq", (session_id,)
        )

    def shots_of_athlete(self, athlete_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT f.* FROM shot_fact f WHERE f.athlete_id=? AND NOT EXISTS "
            "(SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id AND l.session_id=f.session_id "
            "AND l.excluded_at_utc IS NOT NULL) ORDER BY f.shot_time_utc", (athlete_id,)
        )

    def sessions_of_athlete(self, athlete_id: str, include_excluded: bool = False) -> list[sqlite3.Row]:
        if include_excluded:
            return self.sessions_of_athlete_all(athlete_id)
        return self._query(
            "SELECT s.*, (l.excluded_at_utc IS NOT NULL) AS excluded, l.excluded_at_utc "
            "FROM session_dim s LEFT JOIN session_lifecycle l USING (athlete_id, session_id) "
            "WHERE s.athlete_id=? AND l.excluded_at_utc IS NULL ORDER BY s.session_time_utc", (athlete_id,)
        )

    def sessions_of_athlete_all(self, athlete_id: str) -> list[sqlite3.Row]:
        return self._query(
            "SELECT s.*, (l.excluded_at_utc IS NOT NULL) AS excluded, l.excluded_at_utc "
            "FROM session_dim s LEFT JOIN session_lifecycle l USING (athlete_id, session_id) "
            "WHERE s.athlete_id=? ORDER BY s.session_time_utc", (athlete_id,))

    def session_revision(self, session_id: str) -> str:
        """场次内容修订：对库内该场全部箭做确定性哈希（仅引擎计算，桌面回执原样保存）。"""
        digest = hashlib.sha256()
        dim = self.session_of(session_id)
        if dim is not None:
            digest.update(f"dim|{dim['distance_m']}|{dim['mode_composition'] or ''}\n".encode("utf-8"))
        rows = self._query(
            "SELECT * FROM shot_fact WHERE session_id=? ORDER BY shot_seq", (session_id,))
        for row in rows:
            digest.update(("|".join((
                str(row["shot_seq"]),
                f"{float(row['score']):.3f}",
                "1" if row["hit"] else "0",
                "" if row["x_mm"] is None else f"{float(row['x_mm']):.3f}",
                "" if row["y_mm"] is None else f"{float(row['y_mm']):.3f}",
                str(row["shooting_mode"]),
                row["bow_type"] or "",
                row["shot_time_utc"] or "",
                "" if row["mcr_t"] is None else f"{float(row['mcr_t']):.4f}",
                "" if row["hr"] is None else f"{float(row['hr']):.3f}",
                "" if row["wind_speed"] is None else f"{float(row['wind_speed']):.3f}",
                "" if row["wind_dir_deg"] is None else f"{float(row['wind_dir_deg']):.3f}",
            )) + "\n").encode("utf-8"))
        return digest.hexdigest()

    def first_scoring_shot_time(self, session_id: str) -> str | None:
        """场次首支记分箭时间（窗口归属口径，与桌面一致；试射不计入）。"""
        rows = self._query(
            "SELECT MIN(shot_time_utc) AS t FROM shot_fact WHERE session_id=? AND shooting_mode=1",
            (session_id,))
        return rows[0]["t"] if rows else None

    def desktop_sessions_in_window(self, athlete_id: str, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
        """窗口内（按场次首支计分箭时间）未被排除的桌面场次，用于覆盖核对。"""
        return self._query(
            """SELECT s.* FROM session_dim s
               JOIN (SELECT session_id, MIN(shot_time_utc) AS first_scoring_utc
                     FROM shot_fact WHERE shooting_mode=1 GROUP BY session_id) f
                 ON f.session_id=s.session_id
               WHERE s.athlete_id=? AND s.site='desktop'
                 AND f.first_scoring_utc>=? AND f.first_scoring_utc<?
                 AND NOT EXISTS (SELECT 1 FROM session_lifecycle l
                     WHERE l.athlete_id=s.athlete_id AND l.session_id=s.session_id
                       AND l.excluded_at_utc IS NOT NULL)
               ORDER BY f.first_scoring_utc""",
            (athlete_id, start_iso, end_iso))


    def session_lifecycle(self, athlete_id: str, session_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM session_lifecycle WHERE athlete_id=? AND session_id=?",
                           (athlete_id, session_id))
        return rows[0] if rows else None

    def set_session_excluded(self, athlete_id: str, session_id: str, actor_id: str, reason: str) -> tuple[bool, str]:
        """Set exclusion atomically; returns (already_excluded, timestamp)."""
        with self.write_transaction():
            row = self._conn.execute(
                "SELECT excluded_at_utc FROM session_lifecycle WHERE athlete_id=? AND session_id=?",
                (athlete_id, session_id)).fetchone()
            if row and row[0] is not None:
                return True, row[0]
            # The session still participates in comparisons until its lifecycle changes.
            self._invalidate_session_reports(athlete_id, session_id)
            now = _iso_now()
            self._conn.execute(
                "INSERT INTO session_lifecycle(athlete_id,session_id,excluded_at_utc,excluded_by,exclude_reason) "
                "VALUES(?,?,?,?,?) ON CONFLICT(athlete_id,session_id) DO UPDATE SET "
                "excluded_at_utc=excluded.excluded_at_utc, excluded_by=excluded.excluded_by, "
                "exclude_reason=excluded.exclude_reason, restored_at_utc=NULL, restored_by=NULL, restore_reason=NULL",
                (athlete_id, session_id, now, actor_id, reason))
            self._conn.execute(
                "INSERT INTO session_lifecycle_audit(athlete_id,session_id,action,actor_id,reason,occurred_at_utc) "
                "VALUES(?,?,'exclude',?,?,?)", (athlete_id, session_id, actor_id, reason, now))
        return False, now

    def set_session_active(self, athlete_id: str, session_id: str, actor_id: str, reason: str) -> tuple[bool, str]:
        with self.write_transaction():
            row = self._conn.execute(
                "SELECT excluded_at_utc, restored_at_utc FROM session_lifecycle WHERE athlete_id=? AND session_id=?",
                (athlete_id, session_id)).fetchone()
            if not row or row[0] is None:
                return True, (row[1] if row else None) or _iso_now()
            now = _iso_now()
            self._conn.execute(
                "UPDATE session_lifecycle SET excluded_at_utc=NULL, restored_at_utc=?, restored_by=?, restore_reason=? "
                "WHERE athlete_id=? AND session_id=?", (now, actor_id, reason, athlete_id, session_id))
            self._conn.execute(
                "INSERT INTO session_lifecycle_audit(athlete_id,session_id,action,actor_id,reason,occurred_at_utc) "
                "VALUES(?,?,'restore',?,?,?)", (athlete_id, session_id, actor_id, reason, now))
            self._invalidate_session_reports(athlete_id, session_id)
        return False, now

    def is_session_excluded(self, athlete_id: str, session_id: str) -> bool:
        row = self.session_lifecycle(athlete_id, session_id)
        return bool(row and row["excluded_at_utc"])

    # ---- M2b：记忆层 ----


    def shots_in_window(self, athlete_id: str, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
        """窗口箭集：本地时间窗 [start, end) 内该运动员的全部箭。"""
        return self._query(
            """SELECT f.* FROM shot_fact f
               WHERE athlete_id=? AND shot_time_utc>=? AND shot_time_utc<? AND NOT EXISTS
                 (SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id AND l.session_id=f.session_id
                  AND l.excluded_at_utc IS NOT NULL) ORDER BY shot_time_utc""",
            (athlete_id, start_iso, end_iso),
        )

    def sessions_by_ids(self, session_ids: list[str]) -> list[sqlite3.Row]:
        if not session_ids:
            return []
        marks = ",".join("?" * len(session_ids))
        return self._query(
            f"SELECT s.* FROM session_dim s WHERE s.session_id IN ({marks}) AND NOT EXISTS "
            "(SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=s.athlete_id AND l.session_id=s.session_id "
            "AND l.excluded_at_utc IS NOT NULL)", tuple(session_ids))

    def session_of(self, session_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM session_dim WHERE session_id=?", (session_id,))
        return rows[0] if rows else None

    def recent_scoring_shots(self, athlete_id: str, bow_type: str, limit: int) -> list[sqlite3.Row]:
        """最近 N 支记分箭（shooting_mode=1），时间倒序（滚动基线口径）。"""
        return self._query(
            """SELECT * FROM shot_fact
               WHERE athlete_id=? AND bow_type=? AND shooting_mode=1 AND NOT EXISTS
                 (SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=shot_fact.athlete_id
                  AND l.session_id=shot_fact.session_id AND l.excluded_at_utc IS NOT NULL)
               ORDER BY shot_time_utc DESC LIMIT ?""",
            (athlete_id, bow_type, limit),
        )

    def recent_scoring_shots_before(self, athlete_id: str, bow_type: str,
                                    before_iso: str, limit: int) -> list[sqlite3.Row]:
        """窗口起点前最近 N 支记分箭（shooting_mode=1），时间倒序（滚动基线按窗口时点截断）。"""
        return self._query(
            """SELECT * FROM shot_fact
               WHERE athlete_id=? AND bow_type=? AND shooting_mode=1 AND shot_time_utc<? AND NOT EXISTS
                 (SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=shot_fact.athlete_id
                  AND l.session_id=shot_fact.session_id AND l.excluded_at_utc IS NOT NULL)
               ORDER BY shot_time_utc DESC LIMIT ?""",
            (athlete_id, bow_type, before_iso, limit),
        )

    def scoring_shots_of_session(self, session_id: str, bow_type: str | None = None) -> list[sqlite3.Row]:
        """场次记分箭（shooting_mode=1）；指定 bow_type 时只取该弓种（锚点不混弓，ADR-0004）。"""
        if bow_type is None:
            return self._query(
                "SELECT f.* FROM shot_fact f WHERE session_id=? AND shooting_mode=1 AND NOT EXISTS "
                "(SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id AND l.session_id=f.session_id "
                "AND l.excluded_at_utc IS NOT NULL)", (session_id,))
        return self._query(
            "SELECT f.* FROM shot_fact f WHERE session_id=? AND shooting_mode=1 AND bow_type=? AND NOT EXISTS "
            "(SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id AND l.session_id=f.session_id "
            "AND l.excluded_at_utc IS NOT NULL)",
            (session_id, bow_type))

    def bow_types_of_athlete(self, athlete_id: str) -> list[str]:
        rows = self._query(
            "SELECT DISTINCT f.bow_type FROM shot_fact f WHERE f.athlete_id=? AND NOT EXISTS "
            "(SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id AND l.session_id=f.session_id "
            "AND l.excluded_at_utc IS NOT NULL)", (athlete_id,))
        return [r["bow_type"] for r in rows]

    def anchor_candidate_sessions(self, athlete_id: str, bow_type: str) -> list[sqlite3.Row]:
        """可作锚点来源的场次：含该弓种记分箭，且具备可比性元数据（distance_m/mode_composition）。

        供「建立锚点」引导列出候选（A3 要求来源场次元数据必填）；按时间升序，赛季初场次在前。
        元数据校验与 create_anchor_snapshot 的真值判定对齐（distance_m>0 且 mode_composition 非空），
        避免列出建立时必失败的「死候选」。
        """
        return self._query(
            """SELECT s.session_id, s.session_time_utc, s.distance_m, s.mode_composition,
                      (SELECT COUNT(*) FROM shot_fact f
                       WHERE f.session_id=s.session_id AND f.shooting_mode=1 AND f.bow_type=? AND NOT EXISTS
                         (SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id
                          AND l.session_id=f.session_id AND l.excluded_at_utc IS NOT NULL))
                      AS scoring_shots
               FROM session_dim s
               WHERE s.athlete_id=? AND s.distance_m IS NOT NULL AND s.distance_m > 0
                 AND NOT EXISTS (SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=s.athlete_id
                                 AND l.session_id=s.session_id AND l.excluded_at_utc IS NOT NULL)
                 AND s.mode_composition IS NOT NULL AND s.mode_composition != ''
                 AND EXISTS (SELECT 1 FROM shot_fact f
                             WHERE f.session_id=s.session_id AND f.shooting_mode=1 AND f.bow_type=? AND NOT EXISTS
                               (SELECT 1 FROM session_lifecycle l WHERE l.athlete_id=f.athlete_id
                                AND l.session_id=f.session_id AND l.excluded_at_utc IS NOT NULL))
               ORDER BY s.session_time_utc""",
            (bow_type, athlete_id, bow_type),
        )

    def athletes_with_last_session(self) -> list[sqlite3.Row]:
        """档案列表 + 最近训练时间（供前端默认选中最近有训练的运动员）。"""
        return self._query(
            "SELECT p.athlete_id, p.name, p.bow_type, p.level, "
            "(SELECT MAX(s.session_time_utc) FROM session_dim s WHERE s.athlete_id = p.athlete_id) "
            "AS last_session_utc FROM athlete_profile p ORDER BY p.name")

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
        sql = """SELECT b.* FROM baseline_snapshots b WHERE athlete_id=? AND snap_type=?
                 AND NOT EXISTS (SELECT 1 FROM session_lifecycle l
                     WHERE l.athlete_id=b.athlete_id AND l.session_id=b.source_session_id
                       AND l.excluded_at_utc IS NOT NULL)"""
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
                 (athlete_id, note_type, content, author_role, actor_id, status, created_at_utc,
                  closed_at_utc, provenance, quote_text, confirmed_at_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                note["athlete_id"],
                note["note_type"],
                note["content"],
                note["author_role"],
                note["actor_id"],
                "active",
                note["created_at_utc"],
                None,
                note.get("provenance", "manual"),
                note.get("quote_text"),
                note.get("confirmed_at_utc"),
            ),
        )
        return cur.lastrowid

    def get_note(self, note_id: int, athlete_id: str) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM memory_notes WHERE id=? AND athlete_id=?", (note_id, athlete_id))
        return rows[0] if rows else None

    def find_active_note(self, athlete_id: str, content: str) -> sqlite3.Row | None:
        rows = self._query(
            "SELECT * FROM memory_notes WHERE athlete_id=? AND status='active' AND content=? "
            "ORDER BY id DESC LIMIT 1", (athlete_id, content))
        return rows[0] if rows else None

    def confirm_note(self, note_id: int, athlete_id: str) -> bool:
        """候选 → 正式记忆；只允许未确认的 active 备注。"""
        cur = self._execute(
            "UPDATE memory_notes SET confirmed_at_utc=? "
            "WHERE id=? AND athlete_id=? AND status='active' AND confirmed_at_utc IS NULL",
            (_iso_now(), note_id, athlete_id),
        )
        return cur.rowcount > 0

    def update_note_content(self, note_id: int, athlete_id: str, content: str) -> bool:
        cur = self._execute(
            "UPDATE memory_notes SET content=? WHERE id=? AND athlete_id=? AND status='active'",
            (content, note_id, athlete_id),
        )
        return cur.rowcount > 0

    def close_note(self, note_id: int, athlete_id: str) -> bool:
        """软删（B1）：DELETE → status='closed'，保留审计；归属校验 WHERE athlete_id=?。"""
        cur = self._execute(
            "UPDATE memory_notes SET status='closed', closed_at_utc=? WHERE id=? AND athlete_id=? AND status='active'",
            (_iso_now(), note_id, athlete_id),
        )
        return cur.rowcount > 0

    def list_notes(self, athlete_id: str, status: str = "active", days: int | None = None,
                   confirmed: bool | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM memory_notes WHERE athlete_id=? AND status=?"
        params: list = [athlete_id, status]
        if confirmed is True:
            sql += " AND confirmed_at_utc IS NOT NULL"
        elif confirmed is False:
            sql += " AND confirmed_at_utc IS NULL"
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

    def latest_memory(self, athlete_id: str, granularity: str, *,
                      rule_pack_version: str | None = None) -> sqlite3.Row | None:
        """历史结论引用（B2/D5）：同粒度、时间最近 1 条 judgement。"""
        rows = self.recent_judgements(athlete_id, granularity, 1, rule_pack_version=rule_pack_version)
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
                          mdc_version: str | None, report_json: str | None = None,
                          *, rule_pack_version: str | None = None) -> None:
        generated_at = _iso_now()
        with self.write_transaction():
            self._conn.execute(
                """INSERT OR REPLACE INTO report_cache
                     (report_id, athlete_id, granularity, window_key, mdc_version, generated_at_utc, report_json,
                      rule_pack_version)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (report_id, athlete_id, granularity, window_key, mdc_version, generated_at,
                 report_json, rule_pack_version),
            )
            record_report_observation(self._conn, report_id, generated_at, rule_pack_version, report_json)

    def fill_report_body(self, report_id: str, report_json: str, generated_at_utc: str) -> None:
        """给 PR #4 之前的旧缓存行补存正文：写 report_json，生成时间与正文一致；report_id、历史结论不变。"""
        with self.write_transaction():
            updated = self._conn.execute(
                "UPDATE report_cache SET report_json=?, generated_at_utc=? WHERE report_id=? AND report_json IS NULL",
                (report_json, generated_at_utc, report_id))
            if updated.rowcount:
                row = self.get_report_row(report_id)
                record_report_observation(self._conn, report_id, generated_at_utc,
                                          row["rule_pack_version"], report_json)

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

    def recent_judgements(self, athlete_id: str, granularity: str, limit: int,
                          with_delta: bool = False, *, rule_pack_version: str | None = None) -> list[sqlite3.Row]:
        """最近 N 条同粒度 judgement（时间倒序）。

        with_delta=True 只取带 delta_value 的判定（锚点重建触发④要求「提升均超 MDC」，
        delta 为空的行不参与）。平台期识别与锚点重建共用此方法。
        """
        sql = ("SELECT m.* FROM report_memories m WHERE athlete_id=? AND granularity=?"
               " AND conclusion_type='judgement'")
        params: tuple = (athlete_id, granularity)
        if with_delta:
            sql += " AND delta_value IS NOT NULL"
        if rule_pack_version is not None:
            sql += " AND EXISTS (SELECT 1 FROM report_cache c WHERE c.report_id=m.report_id AND c.rule_pack_version=?)"
            params += (rule_pack_version,)
        sql += " ORDER BY generated_at_utc DESC LIMIT ?"
        return self._query(sql, (*params, limit))

    def cached_report_by_id(self, report_id: str) -> sqlite3.Row | None:
        """按 report_id 读取缓存行（GET /reports/{id} 校验报告是否仍有效）。"""
        rows = self._query("SELECT * FROM report_cache WHERE report_id=?", (report_id,))
        return rows[0] if rows else None

    def cached_reports_of_athlete(self, athlete_id: str) -> list[sqlite3.Row]:
        """该运动员全部缓存报告，时间倒序（前端「引用报告」列表）。"""
        return self._query(
            "SELECT * FROM report_cache WHERE athlete_id=? ORDER BY generated_at_utc DESC",
            (athlete_id,))

    def latest_report_window(self, athlete_id: str) -> sqlite3.Row | None:
        """最近一份已生成报告的 (granularity, window_key)（对话未指定窗口时的兜底）。"""
        rows = self._query(
            "SELECT granularity, window_key FROM report_cache WHERE athlete_id=? AND report_json IS NOT NULL"
            " ORDER BY generated_at_utc DESC, rowid DESC LIMIT 1", (athlete_id,))
        return rows[0] if rows else None

    def invalidate_session_cache(self, athlete_id: str, session_id: str) -> None:
        """B10①：同 session_id 重新导入 → 失效该场次缓存报告（daily 窗口键 = daily:<session_id>）。"""
        self._execute(
            "DELETE FROM report_cache WHERE athlete_id=? AND granularity='daily' AND window_key=?",
            (athlete_id, f"daily:{session_id}"),
        )

    def _first_scoring_time(self, athlete_id: str, session_id: str) -> str:
        row = self._conn.execute(
            """SELECT shot_time_utc FROM shot_fact
               WHERE athlete_id=? AND session_id=? AND shooting_mode=1 ORDER BY shot_seq LIMIT 1""",
            (athlete_id, session_id)).fetchone()
        return row["shot_time_utc"] if row else ""

    def invalidate_session_reports(self, athlete_id: str, session_id: str) -> None:
        """失效该场次影响的日/周/月/季/年报告（含比较窗口与锚点依赖），供导入重建复用。"""
        with self.write_transaction():
            self._invalidate_session_reports(athlete_id, session_id)

    def _invalidate_session_reports(self, athlete_id: str, session_id: str) -> None:
        """Called inside the lifecycle transaction, with db.lock held."""
        from app.reports.window import WindowKey
        from app.timeutil import parse_iso_utc

        cached_windows = self._conn.execute(
            "SELECT report_id, granularity, window_key, report_json FROM report_cache WHERE athlete_id=?",
            (athlete_id,),
        ).fetchall()
        if not cached_windows:
            return

        # Legacy reports lack a complete index of anchor dependencies across windows.
        if self._conn.execute(
            "SELECT 1 FROM baseline_snapshots WHERE athlete_id=? AND source_session_id=? AND snap_type='anchor'",
            (athlete_id, session_id),
        ).fetchone():
            self._delete_athlete_reports(athlete_id)
            return

        row = self._conn.execute(
            "SELECT session_time_utc, site FROM session_dim WHERE athlete_id=? AND session_id=?",
            (athlete_id, session_id)).fetchone()
        keys = {WindowKey.daily(session_id)}
        if row and row["site"] == "desktop":
            day = WindowKey.desktop_day_of_shot(self._first_scoring_time(athlete_id, session_id))
            if day:
                keys.add(day)
        period_granularities = {cached["granularity"] for cached in cached_windows} & {
            "weekly", "monthly", "quarterly", "yearly",
        }
        if row:
            cfg = get_config()
            shot_times = self._conn.execute(
                "SELECT DISTINCT shot_time_utc FROM shot_fact WHERE athlete_id=? AND session_id=?",
                (athlete_id, session_id),
            ).fetchall()
            timestamps = [shot["shot_time_utc"] for shot in shot_times] or [row["session_time_utc"]]
            for timestamp in timestamps:
                if parse_iso_utc(timestamp):
                    for granularity in period_granularities:
                        window = WindowKey.of_shot(cfg, timestamp, granularity)
                        if window:
                            keys.add(window)
            # Weekly requests can use legacy spellings for the same window.
            starts = {key.bounds(cfg)[0] for key in keys if key.granularity == "weekly"}
            for cached in cached_windows:
                if starts and cached["granularity"] == "weekly":
                    key = WindowKey.parse("weekly", cached["window_key"])
                    try:
                        if key.bounds(cfg)[0] in starts:
                            keys.add(key)
                    except (ValueError, OverflowError):
                        continue
            valid_times = [timestamp for timestamp in timestamps if parse_iso_utc(timestamp)]
            if valid_times:
                self._add_comparison_report_windows(
                    cfg, athlete_id, session_id, row["session_time_utc"],
                    min(valid_times), cached_windows, keys)
        for key in keys:
            self._delete_report_cache(athlete_id, key.granularity, key.text)

    def _add_comparison_report_windows(
        self, cfg: EngineConfig, athlete_id: str, session_id: str, session_time: str,
        earliest_arrow: str, cached_windows: list[sqlite3.Row], keys: set[WindowKey],
    ) -> None:
        from app.reports.window import WindowKey
        from app.timeutil import parse_iso_utc

        direct_keys = set(keys)
        documents = {}
        for cached in cached_windows:
            try:
                body = json.loads(cached["report_json"] or "null")
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            key = WindowKey.parse(cached["granularity"], cached["window_key"])
            documents[cached["report_id"]] = (key, body)
            window_start = body.get("window_start")
            start = parse_iso_utc(window_start) if isinstance(window_start, str) else None
            if key in keys or start is None or not (body.get("metrics") or {}).get("n_shots"):
                continue
            if key.session_id and session_time < window_start:
                prior = self._conn.execute(
                    """SELECT session_id FROM session_dim s WHERE athlete_id=? AND session_time_utc<?
                       AND NOT EXISTS (SELECT 1 FROM session_lifecycle l
                         WHERE l.athlete_id=s.athlete_id AND l.session_id=s.session_id AND l.excluded_at_utc IS NOT NULL)
                       ORDER BY session_time_utc DESC LIMIT 1""",
                    (athlete_id, window_start),
                ).fetchone()
                if prior and prior["session_id"] == session_id:
                    keys.add(key)
                    continue
            if key.granularity != "daily":
                previous = WindowKey.of_shot(
                    cfg, (start - timedelta(milliseconds=1)).isoformat(), key.granularity)
                if previous in direct_keys:
                    keys.add(key)
                    continue
            bow = body.get("bow_type")
            if bow and earliest_arrow < window_start and any(
                shot["session_id"] == session_id for shot in self.recent_scoring_shots_before(
                    athlete_id, bow, window_start, cfg.rolling_window_shots)
            ):
                keys.add(key)
                continue
            anchor_setup = (body.get("coach_extra") or {}).get("anchor_setup") or {}
            if bow and anchor_setup.get("needs_anchor") and session_time < window_start:
                candidates = [
                    candidate for candidate in self.anchor_candidate_sessions(athlete_id, bow)
                    if (candidate["session_time_utc"] or "") < window_start
                    and candidate["scoring_shots"] >= cfg.data_quality.min_shots_for_description
                ][:10]
                if any(candidate["session_id"] == session_id for candidate in candidates):
                    keys.add(key)

        # Plateau sections cite earlier report IDs. Expire those snapshots as well.
        report_keys = {cached["report_id"]: WindowKey.parse(cached["granularity"], cached["window_key"])
                       for cached in cached_windows}
        previous_size = -1
        while len(keys) != previous_size:
            previous_size = len(keys)
            for key, body in documents.values():
                if key in keys:
                    continue
                plateau = (body.get("conclusions") or {}).get("plateau") or {}
                if any(report_keys.get(item.get("report_id")) in keys for item in plateau.get("recent", [])):
                    keys.add(key)

    def _delete_report_cache(self, athlete_id: str, granularity: str, key: str,
                             generated_through: str | None = None) -> None:
        where = "athlete_id=? AND granularity=? AND window_key=?"
        args = (athlete_id, granularity, key)
        if generated_through is not None:
            where += " AND generated_at_utc<=?"
            args += (generated_through,)
        self._conn.execute(
            f"DELETE FROM report_memories WHERE report_id IN (SELECT report_id FROM report_cache WHERE {where})",
            args)
        self._conn.execute(f"DELETE FROM report_cache WHERE {where}", args)

    def _delete_athlete_reports(self, athlete_id: str) -> None:
        self._conn.execute("DELETE FROM report_memories WHERE athlete_id=?", (athlete_id,))
        self._conn.execute("DELETE FROM report_cache WHERE athlete_id=?", (athlete_id,))

    def _purge_legacy_anchor_reports(self) -> None:
        """Invalidate pre-upgrade reports for athletes whose anchor was excluded in the past."""
        with self.write_transaction():
            if self._conn.execute("PRAGMA user_version").fetchone()[0] >= 1:
                return
            rows = self._conn.execute(
                """SELECT DISTINCT b.athlete_id FROM baseline_snapshots b
                   JOIN session_lifecycle_audit a ON a.athlete_id=b.athlete_id AND a.session_id=b.source_session_id
                   WHERE b.snap_type='anchor' AND a.action='exclude'""").fetchall()
            for row in rows:
                self._delete_athlete_reports(row["athlete_id"])
            self._conn.execute("PRAGMA user_version=1")

    def _purge_legacy_calendar_reports(self) -> None:
        """Remove saved daily reports that predate a lifecycle change on upgraded databases."""
        from app.reports.window import WindowKey

        with self.write_transaction():
            rows = self._conn.execute(
                """SELECT s.athlete_id, s.session_id, MAX(a.occurred_at_utc) AS changed_at
                   FROM session_lifecycle_audit a JOIN session_dim s USING (athlete_id, session_id)
                   WHERE s.site='desktop' AND EXISTS (
                       SELECT 1 FROM report_cache c WHERE c.athlete_id=s.athlete_id
                           AND c.granularity='daily' AND c.window_key LIKE 'daily:day:%')
                   GROUP BY s.athlete_id, s.session_id""").fetchall()
            for row in rows:
                athlete_id = row["athlete_id"]
                day = WindowKey.desktop_day_of_shot(self._first_scoring_time(athlete_id, row["session_id"]))
                if day:
                    self._delete_report_cache(athlete_id, "daily", day.text, row["changed_at"])


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
