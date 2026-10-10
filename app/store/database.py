# -*- coding: utf-8 -*-
"""事实层 + 记忆层数据库访问（SQLite，WAL / Row factory / 幂等 / 单例降级）。

借鉴参考项目 store 经验：WAL、跨线程连接、Row factory、单例缓存、初始化失败返回降级。
M2：shot_fact/session_dim 读写；M2b：记忆表读写方法随后并入（profile/baseline/notes/memories）。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from app.config import EngineConfig, get_config
from app.reports.window import WindowKey
from app.store.schema import SCHEMA_DDL
from app.timeutil import iso_now_utc

logger = logging.getLogger("engine.store")

_SINGLETON: "Database | None" = None


class Database:
    """SQLite 访问封装。连接按线程创建（check_same_thread=False + 调用方串行），WAL 模式。"""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA_DDL)
        self._migrate_columns()
        self._conn.commit()

    # ---- 连接管理 ----

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ---- 列级迁移（CREATE TABLE IF NOT EXISTS 不补列；对已存在库幂等补列）----

    _ADD_COLUMNS: dict[str, list[tuple[str, str]]] = {
        "memory_notes": [
            ("provenance", "TEXT"),
            ("quote_text", "TEXT"),
            ("confirmed_at_utc", "TEXT"),
        ],
    }

    def _migrate_columns(self) -> None:
        for table, cols in self._ADD_COLUMNS.items():
            existing = {row["name"] for row in self._query(f"PRAGMA table_info({table})")}
            for name, ddl in cols:
                if name not in existing:
                    self._execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

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
            )
            for r in rows
        ]
        cur = self._conn.executemany(
            """INSERT OR IGNORE INTO shot_fact
                 (athlete_id, session_id, shot_seq, score, hit, x_mm, y_mm, mcr_t, hr,
                  wind_speed, wind_dir_deg, shooting_mode, bow_type, video_ref, shot_time_utc)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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

    def shots_in_window(self, athlete_id: str, start_iso: str, end_iso: str) -> list[sqlite3.Row]:
        """窗口箭集：本地时间窗 [start, end) 内该运动员的全部箭。"""
        return self._query(
            """SELECT * FROM shot_fact
               WHERE athlete_id=? AND shot_time_utc>=? AND shot_time_utc<? ORDER BY shot_time_utc""",
            (athlete_id, start_iso, end_iso),
        )

    def sessions_by_ids(self, session_ids: list[str]) -> list[sqlite3.Row]:
        if not session_ids:
            return []
        marks = ",".join("?" * len(session_ids))
        return self._query(
            f"SELECT * FROM session_dim WHERE session_id IN ({marks})", tuple(session_ids))

    def session_of(self, session_id: str) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM session_dim WHERE session_id=?", (session_id,))
        return rows[0] if rows else None

    def recent_scoring_shots(self, athlete_id: str, bow_type: str, limit: int) -> list[sqlite3.Row]:
        """最近 N 支记分箭（shooting_mode=1），时间倒序（滚动基线口径）。"""
        return self._query(
            """SELECT * FROM shot_fact
               WHERE athlete_id=? AND bow_type=? AND shooting_mode=1
               ORDER BY shot_time_utc DESC LIMIT ?""",
            (athlete_id, bow_type, limit),
        )

    def recent_scoring_shots_before(self, athlete_id: str, bow_type: str,
                                    before_iso: str, limit: int) -> list[sqlite3.Row]:
        """窗口起点前最近 N 支记分箭（shooting_mode=1），时间倒序（滚动基线按窗口时点截断）。"""
        return self._query(
            """SELECT * FROM shot_fact
               WHERE athlete_id=? AND bow_type=? AND shooting_mode=1 AND shot_time_utc<?
               ORDER BY shot_time_utc DESC LIMIT ?""",
            (athlete_id, bow_type, before_iso, limit),
        )

    def scoring_shots_of_session(self, session_id: str, bow_type: str | None = None) -> list[sqlite3.Row]:
        """场次记分箭（shooting_mode=1）；指定 bow_type 时只取该弓种（锚点不混弓，ADR-0004）。"""
        if bow_type is None:
            return self._query(
                "SELECT * FROM shot_fact WHERE session_id=? AND shooting_mode=1", (session_id,))
        return self._query(
            "SELECT * FROM shot_fact WHERE session_id=? AND shooting_mode=1 AND bow_type=?",
            (session_id, bow_type))

    def bow_types_of_athlete(self, athlete_id: str) -> list[str]:
        rows = self._query(
            "SELECT DISTINCT bow_type FROM shot_fact WHERE athlete_id=?", (athlete_id,))
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
                       WHERE f.session_id=s.session_id AND f.shooting_mode=1 AND f.bow_type=?)
                      AS scoring_shots
               FROM session_dim s
               WHERE s.athlete_id=? AND s.distance_m IS NOT NULL AND s.distance_m > 0
                 AND s.mode_composition IS NOT NULL AND s.mode_composition != ''
                 AND EXISTS (SELECT 1 FROM shot_fact f
                             WHERE f.session_id=s.session_id AND f.shooting_mode=1 AND f.bow_type=?)
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
                profile.get("identity_id"),
                profile.get("name"),
                profile.get("gender"),
                profile.get("age"),
                profile.get("bow_type"),
                profile.get("hand"),
                profile.get("level"),
                iso_now_utc(),
            ),
        )

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
            (iso_now_utc(), note_id, athlete_id),
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

    def recent_judgements(self, athlete_id: str, granularity: str, limit: int,
                          with_delta: bool = False) -> list[sqlite3.Row]:
        """最近 N 条同粒度 judgement（时间倒序）。

        with_delta=True 只取带 delta_value 的判定（锚点重建触发④要求「提升均超 MDC」，
        delta 为空的行不参与）。平台期识别与锚点重建共用此方法。
        """
        sql = ("SELECT * FROM report_memories WHERE athlete_id=? AND granularity=?"
               " AND conclusion_type='judgement'")
        if with_delta:
            sql += " AND delta_value IS NOT NULL"
        sql += " ORDER BY generated_at_utc DESC LIMIT ?"
        return self._query(sql, (athlete_id, granularity, limit))

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

    def _cached_report_ids(self, athlete_id: str, granularity: str, window_key: str,
                           mdc_version: str | None) -> list[str]:
        """同窗口缓存报告 id（口径版本匹配：NULL 只匹配 NULL）。查找与清理共用。"""
        rows = self._query(
            """SELECT report_id FROM report_cache
               WHERE athlete_id=? AND granularity=? AND window_key=?
                 AND (mdc_version IS ? OR mdc_version=?)""",
            (athlete_id, granularity, window_key, mdc_version, mdc_version),
        )
        return [r["report_id"] for r in rows]

    def get_cached_report(self, athlete_id: str, granularity: str, window_key: str, mdc_version: str | None) -> str | None:
        ids = self._cached_report_ids(athlete_id, granularity, window_key, mdc_version)
        return ids[0] if ids else None

    def put_cached_report(self, report_id: str, athlete_id: str, granularity: str, window_key: str, mdc_version: str | None) -> None:
        self._execute(
            """INSERT OR REPLACE INTO report_cache
                 (report_id, athlete_id, granularity, window_key, mdc_version, generated_at_utc)
               VALUES (?,?,?,?,?,?)""",
            (report_id, athlete_id, granularity, window_key, mdc_version, iso_now_utc()),
        )

    def purge_report_window(self, athlete_id: str, granularity: str, window_key: str,
                            mdc_version: str | None) -> None:
        """重生成前清理同窗口旧缓存行及其结论记忆。

        report_id 是 uuid 主键，INSERT OR REPLACE 永远只会追加，同窗口重生成会堆积（周报实测 37 行）：
        前端「引用报告」下拉被重复项撑爆，且「近 3 期 / 近 4 周」判定会读到同一份报告的重复行。
        """
        for rid in self._cached_report_ids(athlete_id, granularity, window_key, mdc_version):
            self._execute("DELETE FROM report_memories WHERE report_id=?", (rid,))
        self._execute(
            """DELETE FROM report_cache
               WHERE athlete_id=? AND granularity=? AND window_key=?
                 AND (mdc_version IS ? OR mdc_version=?)""",
            (athlete_id, granularity, window_key, mdc_version, mdc_version))

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
            "SELECT granularity, window_key FROM report_cache WHERE athlete_id=?"
            " ORDER BY generated_at_utc DESC LIMIT 1", (athlete_id,))
        return rows[0] if rows else None

    def invalidate_session_cache(self, athlete_id: str, session_id: str) -> None:
        """B10①：同 session_id 重新导入 → 失效该场次缓存报告（daily 窗口键 = daily:<session_id>）。"""
        self._execute(
            "DELETE FROM report_cache WHERE athlete_id=? AND granularity='daily' AND window_key=?",
            (athlete_id, WindowKey.daily(session_id).text),
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
