# -*- coding: utf-8 -*-
"""PR #4：display_no（引擎本地顺序号）、最近靶位 lane、v1.2 新运动员不再自动命名、旧库迁移。"""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store.database import Database, get_database
from tests.test_v12_ingest import URL, make_dt2

STATIC = Path(__file__).resolve().parent.parent / "static" / "index.html"


@pytest.fixture()
def client(engine_env):
    return TestClient(app)


def _aid_of(spid: str) -> str:
    from app.ingest.identity import pseudonymize_athlete
    return pseudonymize_athlete(spid)


class TestDisplayNo:
    def test_sequential_by_insertion_not_by_id(self, db):
        for aid in ("999", "111", "555"):
            db.upsert_profile({"athlete_id": aid, "name": aid})
        got = {r["athlete_id"]: r["display_no"] for r in db.query("SELECT * FROM athlete_profile")}
        assert got == {"999": 1, "111": 2, "555": 3}

    def test_stable_on_upsert(self, db):
        db.upsert_profile({"athlete_id": "A", "name": "甲"})
        db.upsert_profile({"athlete_id": "B", "name": "乙"})
        db.upsert_profile({"athlete_id": "A", "name": "甲改", "bow_type": "复合弓"})
        assert db.get_profile("A")["display_no"] == 1 and db.get_profile("B")["display_no"] == 2

    def test_put_profile_cannot_change(self, client):
        client.post("/api/v1/ingest/mock")
        aid = "1963169497552654337"
        before = client.get(f"/api/v1/athletes/{aid}/profile").json()["display_no"]
        r = client.put(f"/api/v1/athletes/{aid}/profile", json={"name": "张明", "display_no": 99})
        assert r.status_code == 200
        assert client.get(f"/api/v1/athletes/{aid}/profile").json()["display_no"] == before

    def test_no_relation_to_identity(self, tmp_path):
        """同一运动员在两个库里按出现顺序编号：编号只取决于建档先后，与 athlete_id 无关。"""
        d1, d2 = Database(str(tmp_path / "a.db")), Database(str(tmp_path / "b.db"))
        for aid in ("X", "Y"):
            d1.upsert_profile({"athlete_id": aid})
        for aid in ("Y", "X"):
            d2.upsert_profile({"athlete_id": aid})
        assert d1.get_profile("X")["display_no"] == 1 and d2.get_profile("X")["display_no"] == 2
        d1.close(); d2.close()

    def test_unique_index(self, db):
        db.upsert_profile({"athlete_id": "A"})
        db.upsert_profile({"athlete_id": "B"})
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute("UPDATE athlete_profile SET display_no=1 WHERE athlete_id='B'")


class TestMigration:
    def _legacy_db(self, path: Path) -> None:
        """PR #4 之前的库：athlete_profile 没有 display_no，report_cache 没有 report_json。"""
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE athlete_profile (athlete_id TEXT PRIMARY KEY, account_id TEXT, identity_id TEXT,
              name TEXT, gender TEXT, age INTEGER, bow_type TEXT, hand TEXT, level TEXT, updated_at_utc TEXT);
            CREATE TABLE report_cache (report_id TEXT PRIMARY KEY, athlete_id TEXT NOT NULL,
              granularity TEXT NOT NULL, window_key TEXT NOT NULL, mdc_version TEXT, generated_at_utc TEXT NOT NULL);
            INSERT INTO athlete_profile (athlete_id, name) VALUES ('8000000000000004321', '运动员4321');
            INSERT INTO athlete_profile (athlete_id, name) VALUES ('1000000000000001111', '张伟');
            INSERT INTO athlete_profile (athlete_id, name) VALUES ('5000000000000009999', '运动员1234');
            INSERT INTO report_cache VALUES ('R1', '1000000000000001111', 'weekly', '2026-W32', NULL, '2026-08-10T00:00:00.000Z');
        """)
        conn.commit(); conn.close()

    def test_legacy_db_migrated_idempotent(self, engine_env, tmp_path):
        path = tmp_path / "legacy.db"
        self._legacy_db(path)
        for _ in range(2):  # 两次启动结果一致
            d = Database(str(path))
            rows = {r["athlete_id"]: dict(r) for r in d.query("SELECT * FROM athlete_profile")}
            assert [rows[a]["display_no"] for a in ("8000000000000004321", "1000000000000001111",
                                                   "5000000000000009999")] == [1, 2, 3]  # 按插入顺序
            assert rows["8000000000000004321"]["name"] is None       # 恰好是「运动员+后四位」→ 清空
            assert rows["1000000000000001111"]["name"] == "张伟"
            assert rows["5000000000000009999"]["name"] == "运动员1234"  # 后四位不匹配 → 不动
            cols = {r["name"] for r in d.query("PRAGMA table_info(report_cache)")}
            assert "report_json" in cols
            assert d.get_report_row("R1")["report_json"] is None
            idx = {r["name"] for r in d.query("PRAGMA index_list(athlete_profile)")}
            assert "idx_profile_display_no" in idx
            d.close()

    def test_new_profile_after_migration_continues(self, engine_env, tmp_path):
        path = tmp_path / "legacy.db"
        self._legacy_db(path)
        d = Database(str(path))
        d.upsert_profile({"athlete_id": "NEW"})
        assert d.get_profile("NEW")["display_no"] == 4
        d.close()


class TestV12Unnamed:
    def test_new_v12_athlete_has_no_name(self, client):
        r = client.post(URL, json=[make_dt2(i) for i in range(1, 4)])
        assert r.json()["summary"]["accepted"] == 3
        aid = _aid_of("spid-test-a")
        prof = get_database().get_profile(aid)
        assert prof["name"] is None
        assert prof["display_no"] >= 1
        a = [x for x in client.get("/api/v1/athletes").json()["athletes"] if x["athlete_id"] == aid][0]
        assert a["name"] is None
        assert a["display_no"] == prof["display_no"]

    def test_existing_name_not_overwritten(self, client):
        aid = _aid_of("spid-test-a")
        get_database().upsert_profile({"athlete_id": aid, "name": "李明"})
        client.post(URL, json=[make_dt2(1)])
        assert get_database().get_profile(aid)["name"] == "李明"

    def test_engine_page_handles_empty_name(self):
        html = STATIC.read_text(encoding="utf-8")
        assert "(a.name || '未命名选手')" in html
        assert "(r.athlete.name || '未命名选手')" in html

    def test_report_with_unnamed_athlete(self, client):
        client.post(URL, json=[make_dt2(i) for i in range(1, 25)])
        aid = _aid_of("spid-test-a")
        sid = get_database().query("SELECT session_id FROM session_dim WHERE athlete_id=?", (aid,))[0]["session_id"]
        rep = client.post(f"/api/v1/athletes/{aid}/reports/daily", params={"session_id": sid}).json()
        assert rep["athlete"] == {"id": aid, "name": None}


class TestLane:
    def test_list_fields_null_for_mock(self, client):
        client.post("/api/v1/ingest/mock")
        athletes = client.get("/api/v1/athletes").json()["athletes"]
        assert athletes
        for a in athletes:
            assert {"display_no", "lane", "lane_seen_at_utc"} <= set(a)
            assert isinstance(a["display_no"], int)
            assert a["lane"] is None and a["lane_seen_at_utc"] is None  # mock 源没有靶位
        nos = [a["display_no"] for a in athletes]
        assert len(set(nos)) == len(nos)

    def test_latest_lane_string(self, client):
        msgs = [make_dt2(1, lane="lane-01"), make_dt2(2, lane="lane-03"), make_dt2(3, lane=None)]
        r = client.post(URL, json=msgs)
        assert r.json()["summary"]["accepted"] == 3, r.json()
        aid = _aid_of("spid-test-a")
        a = [x for x in client.get("/api/v1/athletes").json()["athletes"] if x["athlete_id"] == aid][0]
        assert a["lane"] == "lane-03"  # 最近一支「带靶位」的箭；第 3 支 lane=null 跳过
        assert a["lane_seen_at_utc"] == "2026-09-07T01:01:00.000Z"
        one = client.get(f"/api/v1/athletes/{aid}").json()
        assert one["lane"] == "lane-03" and one["lane_seen_at_utc"] == a["lane_seen_at_utc"]
        assert one["profile"]["display_no"] == a["display_no"]
        assert client.get(f"/api/v1/athletes/{aid}/profile").json()["display_no"] == a["display_no"]

    def test_unknown_athlete_lane_null(self, client):
        one = client.get("/api/v1/athletes/NOPE").json()
        assert one["lane"] is None and one["lane_seen_at_utc"] is None and one["profile"] is None
