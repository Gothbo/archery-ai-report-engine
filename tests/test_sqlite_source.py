# -*- coding: utf-8 -*-
"""M5：真 SQLite 数据源测试（伪造 display_sys 同构小库，验证语义映射/时间转换/匹配/聚类）。"""
import sqlite3

import pytest

from app.ingest import ingest_source
from app.ingest.sqlite_source import SQLiteSource, athlete_id_of
from app.memory.baseline import refresh_rolling_after_import
from app.store.database import Database

# 合成测试身份号（非真实证件号）：纯数字 18 位 / 含 X 校验位
IDENT = "000000200001010011"
IDENT_X = "00000020000202002X"
NAME = "测试员甲"


def aid() -> str:
    """athlete_id 依赖脱敏密钥（随测试临时库目录生成），须在夹具生效后计算，不能在导入期求值。"""
    return athlete_id_of(IDENT)


def aid_x() -> str:
    return athlete_id_of(IDENT_X)


def _make_fake_real_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ScoreInfoNew (
            Id INTEGER, Score TEXT, IsGood INTEGER, X_ TEXT, Y_ TEXT,
            ProjectId INTEGER, ShotType INTEGER, ShootingTime TEXT, IdentityID TEXT,
            RegisterNum TEXT, MatchType TEXT, Num INTEGER);
        CREATE TABLE HeartRateData (
            IdentityID TEXT, HeartRate INTEGER, CreateDate TEXT);
        CREATE TABLE WindSpeedDirection (
            RegisterNum TEXT, AthleteName TEXT, WindSpeed REAL, WindDirection TEXT, CreateTime TEXT);
    """)
    # 运动员 A：07:00 试射场（与 09:00 间隔 2h>90min → 拆场）；09:00 记分场 5 箭；11:00 场 2 箭
    conn.executemany("""INSERT INTO ScoreInfoNew
        (Id, Score, IsGood, X_, Y_, ProjectId, ShotType, ShootingTime, IdentityID, RegisterNum, MatchType, Num)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", [
        (1, "9.10", 0, "5.0", "5.0", 171, 1, "2024-06-20 07:00:00.000", IDENT, IDENT, "Qualification", 0),
        (2, "9.30", 0, "86.2", "-62.8", 171, 3, "2024-06-20 09:00:00.000", IDENT, IDENT, "Qualification", 1),
        (3, "10.90", 1, "10.5", "3.2", 171, 3, "2024-06-20 09:00:20.000", IDENT, IDENT, "Qualification", 2),
        (4, "8.10", 0, "-40.1", "22.5", 171, 3, "2024-06-20 09:00:40.000", IDENT, IDENT, "Qualification", 3),
        (5, "0.0", 0, "0.0", "0.0", 171, 3, "2024-06-20 09:01:00.000", IDENT, IDENT, "Qualification", 4),
        (6, "9.50", 0, "1.0", "-1.0", 171, 3, "2024-06-20 09:02:30.000", IDENT, IDENT, "Qualification", 5),
        (7, "7.90", 0, "-88.4", "28.9", 171, 7, "2024-06-20 11:00:00.000", IDENT, IDENT, "Qualification", 6),
        (8, "9.60", 0, "12.1", "-10.0", 171, 7, "2024-06-20 11:00:20.000", IDENT, IDENT, "Qualification", 7),
        (9, "8.80", 0, "1.0", "2.0", 171, 3, "2024-06-21 09:00:00.000", IDENT_X, IDENT_X, "资格赛", 1),
        (10, "5.00", 0, "0.0", "0.0", 171, 3, "2009-01-01 04:54:58.000", "123456", "123456", "x", 1),
    ])
    # HR：09:00:05(80)、09:00:45(95)；Wind：09:00:02(1.2/194.6)、09:00:42(1.8/186.1)
    conn.executemany("""INSERT INTO HeartRateData (IdentityID, HeartRate, CreateDate) VALUES (?,?,?)""", [
        (IDENT, 80, "2024-06-20 09:00:05.000"),
        (IDENT, 95, "2024-06-20 09:00:45.000"),
    ])
    conn.executemany("""INSERT INTO WindSpeedDirection
        (RegisterNum, AthleteName, WindSpeed, WindDirection, CreateTime) VALUES (?,?,?,?,?)""", [
        (IDENT, NAME, 1.2, "194.6", "2024-06-20 09:00:02.000"),
        (IDENT, NAME, 1.8, "186.1", "2024-06-20 09:00:42.000"),
    ])
    conn.commit()
    conn.close()


@pytest.fixture()
def fake_real_db(engine_env):
    path = engine_env[1].replace("facts.db", "real.db")
    _make_fake_real_db(path)
    return path


def _sessions_of(src: SQLiteSource, aid: str) -> list:
    return [s for s in src.iter_sessions() if s.athlete_id == aid]


class TestAthleteIdMapping:
    """P0 隐私：athlete_id = HMAC(密钥, 身份号) → 19 位数字，不可逆。"""

    def test_format_19_digits(self, engine_env):
        assert len(aid()) == 19 and aid().isdigit()
        assert len(aid_x()) == 19 and aid_x().isdigit()

    def test_not_reversible_legacy_encoding(self, engine_env):
        from app.ingest.identity import legacy_athlete_id
        assert aid() != legacy_athlete_id(IDENT)          # 不再是 10^18 + 号码
        assert IDENT not in aid()
        assert str(int(aid()) - 10**18) != IDENT.lstrip("0")

    def test_deterministic_case_insensitive(self, engine_env):
        assert athlete_id_of(IDENT_X.lower()) == aid_x()
        assert athlete_id_of(f" {IDENT} ") == aid()

    def test_depends_on_secret(self, engine_env, monkeypatch):
        before = aid()
        monkeypatch.setenv("ENGINE_ID_SECRET", "another-secret")
        assert aid() != before

    def test_key_file_created_next_to_db(self, engine_env):
        from pathlib import Path
        aid()
        assert (Path(engine_env[1]).parent / "athlete_id.key").exists()


class TestSqliteSource:
    def test_session_clustering(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        sessions = list(src.iter_sessions())
        # 有效箭 9 条：A 试射场 + A 09:00 场 + A 11:00 场 + X 1 场 = 4 场（2009 行被过滤）
        assert len(sessions) == 4
        a_sessions = _sessions_of(src, aid())
        assert len(a_sessions) == 3
        x_sessions = _sessions_of(src, aid_x())
        assert len(x_sessions) == 1

    def test_session_id_deterministic(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        ids1 = [s.session_id for s in src.iter_sessions()]
        ids2 = [s.session_id for s in src.iter_sessions()]
        assert ids1 == ids2  # 重复导入同 id → 幂等

    def test_session_ids_unique_within_date(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        a_sessions = _sessions_of(src, aid())
        # 同日 3 场 → _01/_02/_03 序号
        suffixes = sorted(s.session_id.split("_")[-1] for s in a_sessions)
        assert suffixes == ["01", "02", "03"]

    def test_time_utc_conversion(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        a_sessions = _sessions_of(src, aid())
        morning = [s for s in a_sessions if s.session_id.endswith("_02")][0]
        # 本地 2024-06-20 09:00:00 (Asia/Shanghai) → UTC 01:00:00Z
        assert morning.shots[0].shot_time_utc == "2024-06-20T01:00:00.000Z"
        trial = [s for s in a_sessions if s.session_id.endswith("_01")][0]
        assert trial.shots[0].shot_time_utc == "2024-06-19T23:00:00.000Z"  # 07:00 本地 = 前日 23:00 UTC

    def test_score_hit_and_mode_mapping(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        a_sessions = _sessions_of(src, aid())
        morning = [s for s in a_sessions if s.session_id.endswith("_02")][0]
        shots = sorted(morning.shots, key=lambda sh: sh.shot_seq)
        assert shots[0].score == 9.3 and shots[0].hit is True
        assert shots[3].score == 0.0 and shots[3].hit is False  # 0.0 = 脱靶
        assert all(sh.shooting_mode == 1 for sh in shots)  # ShotType=3 → 记分
        afternoon = [s for s in a_sessions if s.session_id.endswith("_03")][0]
        assert all(sh.shooting_mode == 1 for sh in afternoon.shots)  # ShotType=7 → 记分
        trial = [s for s in a_sessions if s.session_id.endswith("_01")][0]
        assert trial.shots[0].shooting_mode == 0  # ShotType=1 → 试射

    def test_hr_wind_nearest_match(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        a_sessions = _sessions_of(src, aid())
        morning = [s for s in a_sessions if s.session_id.endswith("_02")][0]
        shots = sorted(morning.shots, key=lambda sh: sh.shot_seq)
        assert shots[0].hr == 80 and shots[0].wind_speed == 1.2 and shots[0].wind_dir_deg == 194.6
        assert shots[2].hr == 95 and shots[2].wind_speed == 1.8  # 09:00:45/09:00:42 最近
        assert shots[3].hr == 95  # 09:01:00 距 09:00:45 仅 15s → 窗口内匹配
        assert shots[4].hr is None and shots[4].wind_speed is None  # 09:02:30 超窗 → 缺测
        afternoon = [s for s in a_sessions if s.session_id.endswith("_03")][0]
        assert afternoon.shots[0].hr is None and afternoon.shots[0].wind_speed is None

    def test_bow_type_mapping(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        for s in src.iter_sessions():
            assert s.shots[0].bow_type == "反曲弓"  # ProjectId=171 映射

    def test_athlete_profiles_from_wind(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        profiles = src.athlete_profiles()
        assert profiles[aid()]["name"] == NAME
        assert "identity_id" not in profiles[aid()]  # 身份证号不进档案

    def test_2009_junk_filtered(self, fake_real_db):
        src = SQLiteSource(fake_real_db)
        total = sum(1 for _ in src.iter_sessions())
        assert total == 4  # 2009 行（6 位身份号 + 早于 min_time）被过滤


def _make_interleaved_db(path: str, reverse_insert: bool = False, athletes=(IDENT, IDENT_X)) -> None:
    """多人同时段射箭：每人 10 箭、20s 一箭，行按时间交错（A,X,A,X…）。"""
    rows = []
    for i in range(10):
        for j, ident in enumerate(athletes):
            ts = f"2024-06-22 09:{(i * 20) // 60:02d}:{(i * 20) % 60:02d}.{j:03d}"
            rows.append((len(rows) + 1, "9.00", 0, "1.0", "1.0", 171, 3, ts, ident, ident, "Qualification", i + 1))
    if reverse_insert:  # 物理行序打乱 → 结果应完全一致
        rows.reverse()
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE ScoreInfoNew (
            Id INTEGER, Score TEXT, IsGood INTEGER, X_ TEXT, Y_ TEXT,
            ProjectId INTEGER, ShotType INTEGER, ShootingTime TEXT, IdentityID TEXT,
            RegisterNum TEXT, MatchType TEXT, Num INTEGER);
        CREATE TABLE HeartRateData (IdentityID TEXT, HeartRate INTEGER, CreateDate TEXT);
        CREATE TABLE WindSpeedDirection (
            RegisterNum TEXT, AthleteName TEXT, WindSpeed REAL, WindDirection TEXT, CreateTime TEXT);
    """)
    conn.executemany("""INSERT INTO ScoreInfoNew
        (Id, Score, IsGood, X_, Y_, ProjectId, ShotType, ShootingTime, IdentityID, RegisterNum, MatchType, Num)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    conn.close()


class TestSqliteInterleavedAthletes:
    """回归：多人同时段、行交错时，场次不能按相邻行被切碎成单箭场。"""

    def _summary(self, path: str) -> list[tuple[str, str, int, tuple]]:
        return [(s.session_id, s.athlete_id, len(s.shots),
                 tuple((sh.shot_seq, sh.shot_time_utc) for sh in s.shots))
                for s in SQLiteSource(path).iter_sessions()]

    def test_two_athletes_interleaved_two_sessions(self, engine_env):
        path = engine_env[1].replace("facts.db", "interleaved.db")
        _make_interleaved_db(path)
        sessions = list(SQLiteSource(path).iter_sessions())
        assert len(sessions) == 2
        assert sorted(s.athlete_id for s in sessions) == sorted([aid(), aid_x()])
        for s in sessions:
            assert len(s.shots) == 10
            assert [sh.shot_seq for sh in s.shots] == list(range(1, 11))
            assert all(sh.athlete_id == s.athlete_id and sh.session_id == s.session_id for sh in s.shots)
        assert {s.session_id for s in sessions} == {f"{aid()}_20240622_01", f"{aid_x()}_20240622_01"}

    def test_interleaved_session_ids_stable(self, engine_env):
        path = engine_env[1].replace("facts.db", "interleaved.db")
        path_rev = engine_env[1].replace("facts.db", "interleaved_rev.db")
        _make_interleaved_db(path)
        _make_interleaved_db(path_rev, reverse_insert=True)
        first = self._summary(path)
        assert first == self._summary(path)       # 重复导入同结果
        assert first == self._summary(path_rev)   # 物理行序不同也同结果

    def test_single_athlete_unchanged_by_other_athlete(self, engine_env):
        """同一运动员的场次不因他人交错而改变（与单人库结果一致）。"""
        path = engine_env[1].replace("facts.db", "interleaved.db")
        path_solo = engine_env[1].replace("facts.db", "solo.db")
        _make_interleaved_db(path)
        _make_interleaved_db(path_solo, athletes=(IDENT,))
        mixed = [x for x in self._summary(path) if x[1] == aid()]
        solo = self._summary(path_solo)
        assert len(solo) == 1 and solo[0][2] == 10
        assert [(sid, n) for sid, _, n, _ in mixed] == [(sid, n) for sid, _, n, _ in solo]


class TestSqliteIngestPipeline:
    def test_ingest_sqlite_source_end_to_end(self, fake_real_db, engine_env):
        db = Database(engine_env[1])
        src = SQLiteSource(fake_real_db)
        stats = ingest_source(db, src, on_imported=refresh_rolling_after_import)
        assert stats["inserted_shots"] == 9
        assert stats["skipped_shots"] == 0
        # 幂等重导
        stats2 = ingest_source(db, SQLiteSource(fake_real_db), on_imported=refresh_rolling_after_import)
        assert stats2["inserted_shots"] == 0
        # 滚动基线：A 记分箭 = 5(09:00场) + 2(11:00场) = 7（试射不纳入）
        snap = db.latest_snapshot(aid(), "rolling")
        assert snap is not None and snap["n_shots"] == 7
        # 档案从风表写入
        profile = db.get_profile(aid())
        assert profile["name"] == NAME
        assert profile["identity_id"] is None  # 身份证号不落库
        db.close()


class TestSqliteMissingSentinels:
    """缺测哨兵（接口 v1.2 §十）：HeartRate=0 / WindSpeed=-1 不参与最近邻匹配；风向缺失不当 0°。"""

    def test_hr_zero_and_wind_negative_skipped(self, fake_real_db):
        conn = sqlite3.connect(fake_real_db)
        conn.execute("INSERT INTO HeartRateData VALUES (?,?,?)", (IDENT, 0, "2024-06-20 09:00:20.000"))
        conn.execute("INSERT INTO WindSpeedDirection VALUES (?,?,?,?,?)",
                     (IDENT, NAME, -1, "-1", "2024-06-20 09:00:20.000"))
        conn.execute("INSERT INTO WindSpeedDirection VALUES (?,?,?,?,?)",
                     (IDENT, NAME, 0.0, None, "2024-06-20 11:00:00.000"))
        conn.commit()
        conn.close()
        src = SQLiteSource(fake_real_db)
        a_sessions = _sessions_of(src, aid())
        morning = [s for s in a_sessions if s.session_id.endswith("_02")][0]
        shots = sorted(morning.shots, key=lambda sh: sh.shot_seq)
        assert shots[1].hr == 80             # 09:00:20 的 0 是缺测 → 取 09:00:05 的 80
        assert shots[1].wind_speed == 1.2    # 09:00:20 的 -1 是缺测 → 取 09:00:02 的 1.2
        afternoon = [s for s in a_sessions if s.session_id.endswith("_03")][0]
        assert afternoon.shots[0].wind_speed == 0.0      # 0 = 真实无风，保留
        assert afternoon.shots[0].wind_dir_deg is None   # 风向缺失 → NULL（不当 0°）
