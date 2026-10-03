# -*- coding: utf-8 -*-
"""v1.2 上行接口接收适配层：校验 / 去重 / 分发 / 字段映射 / 缺测 / 端到端出报告。

样例来源（tests/fixtures/v12，逐字节复制自 PM 侧对接包与 mock 数据，无真实身份信息）：
- doc_examples.json         对接包《样例.json》10 条（dt1–dt8 + dt9A/9B）
- mock_dt5_full.json / mock_dt6.json / mock_dt5_dt6_demo.jsonc   拉力/撒放 mock
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ingest.v12.validate import validate_message

FIX = Path(__file__).resolve().parent / "fixtures" / "v12"
URL = "/api/v1/ingest/v12"


def _examples() -> list[dict]:
    return json.loads((FIX / "doc_examples.json").read_text(encoding="utf-8"))


def _jsonc_objects(path: Path) -> list[dict]:
    text = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
    dec, out, i = json.JSONDecoder(), [], 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return out
        obj, i = dec.raw_decode(text, i)
        out.append(obj)


def _mock_messages() -> list[dict]:
    return [json.loads((FIX / "mock_dt5_full.json").read_text(encoding="utf-8")),
            json.loads((FIX / "mock_dt6.json").read_text(encoding="utf-8")),
            *_jsonc_objects(FIX / "mock_dt5_dt6_demo.jsonc")]


def _by_dt(dt: int) -> dict:
    return copy.deepcopy(next(m for m in _examples() if m["dataType"] == dt))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def make_dt2(n: int, *, athlete: str = "spid-test-a", base: str = "2026-09-07T01:00:00.000Z",
             step_s: int = 30, score: int = 9, hr: float = 90, wind: float = 1.0, seq0: int = 1,
             mid_prefix: str = "m", shot_base: int = 1963169497552700000, **over) -> dict:
    """第 n 支箭的合法 dt2 消息（各字段按 schema 必填项给全）。"""
    t0 = datetime.fromisoformat(base.replace("Z", "+00:00")) + timedelta(seconds=step_s * n)
    shot_id = str(shot_base + n)
    data = {
        "releaseTime": _iso(t0), "hitTime": _iso(t0 + timedelta(milliseconds=1200)), "flightTimeMs": 1200,
        "shotSeq": seq0 + n, "shotId": shot_id, "scoreId": shot_id, "lane": "lane-01",
        "athleteId": athlete, "score": score, "x": 1.5, "y": -2.0, "innerTen": False,
        "shootingMode": 1, "bowType": "recurve", "targetType": "122cm-recurve",
        "heartRate": hr, "windSpeed": wind, "windDirection": 90, "offsetM": 0.05,
    }
    data.update(over)
    return {"schemaVersion": "1.1", "messageId": f"{mid_prefix}-{shot_id}", "dataType": 2, "data": data}


@pytest.fixture()
def client(engine_env):
    return TestClient(__import__("app.main", fromlist=["app"]).app)


def _db():
    from app.store.database import get_database
    return get_database()


def _post(client, payload):
    r = client.post(URL, json=payload)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 校验

class TestValidation:
    def test_vendored_schema_is_doc_copy(self):
        import hashlib
        from app.ingest.v12.validate import SCHEMA_PATH, SCHEMA_SHA256
        assert hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest() == SCHEMA_SHA256

    def test_doc_examples_all_valid(self):
        msgs = _examples()
        assert len(msgs) == 10
        for m in msgs:
            assert validate_message(m) == [], m["messageId"]

    def test_mock_data_all_valid(self):
        msgs = _mock_messages()
        assert len(msgs) == 4
        assert len(msgs[0]["data"]["curve"]) == 4101
        for m in msgs:
            assert validate_message(m) == [], m["messageId"]

    @pytest.mark.parametrize("mutate, expect", [
        # 文档 §十 脱靶样例：score=0 + scoreId=null → schema 不接受（文档/schema 矛盾，见 PR 说明）
        (lambda m: m["data"].update(score=0, innerTen=False, scoreId=None), "score"),
        # 文档 §十 releaseTime 缺失降级样例 → schema 必填
        (lambda m: m["data"].pop("releaseTime"), "releaseTime"),
        # +08:00 偏移：schema format 放行，但文档硬约束要求 UTC Z → 本适配层拒绝
        (lambda m: m["data"].update(releaseTime="2025-09-09T18:18:44.620+08:00"), "releaseTime"),
        (lambda m: m.update(sentAt="2025-09-09 10:18:45"), "sentAt"),
        (lambda m: m.update(schemaVersion="1.2"), "schemaVersion"),
        (lambda m: m["data"].update(shotId=1963169497552654336), "shotId"),   # ID 不能是 number
        (lambda m: m["data"].update(score=10.7), "score"),                     # 环值须整数
        (lambda m: m["data"].update(bowType="反曲弓"), "bowType"),
        (lambda m: m["data"].update(lane=1), "lane"),
        (lambda m: m.update(subType="athleteProfile"), "subType"),
        (lambda m: m.pop("messageId"), "messageId"),
    ])
    def test_dt2_invalid_cases(self, mutate, expect):
        m = _by_dt(2)
        mutate(m)
        errs = validate_message(m)
        assert errs and any(expect in e for e in errs), errs

    def test_realtime_mixed_null_rejected(self):
        m = _by_dt(1)
        m["data"]["shotId"] = None          # shotSeq 仍为 1：schema 放行，文档 §3.9 禁止
        errs = validate_message(m)
        assert any("同步可空" in e for e in errs)
        m["data"]["shotSeq"] = None         # 两者同步为 null：合法（背景流）
        assert validate_message(m) == []

    def test_realtime_release_time_forbidden(self):
        m = _by_dt(4)
        m["data"]["releaseTime"] = "2025-09-09T10:18:44.620Z"
        assert validate_message(m)

    def test_unknown_datatype_and_non_object(self):
        m = _by_dt(2)
        m["dataType"] = 10
        assert validate_message(m)
        assert validate_message([1, 2]) == ["消息必须是 JSON 对象"]


# ---------------------------------------------------------------- 接收 / 去重 / 分发

class TestReceiver:
    def test_doc_examples_batch(self, client):
        out = _post(client, _examples())
        st = {r["messageId"]: r["status"] for r in out["results"]}
        for mid in ("msg_001", "msg_002", "msg_003", "msg_004", "msg_005", "msg_006", "msg_007"):
            assert st[mid] == "accepted", out["results"]
        for mid in ("msg_008", "msg_009", "msg_010"):
            assert st[mid] == "unsupported"
        assert out["summary"]["accepted"] == 7 and out["summary"]["unsupported"] == 3
        assert out["summary"]["sessions_rebuilt"] == 1
        assert [r["index"] for r in out["results"]] == list(range(10))
        db = _db()
        # dt9A 档案（含 name）不在本期：不落库
        assert db.query("SELECT COUNT(*) c FROM ingest_messages")[0]["c"] == 7
        assert db.query("SELECT COUNT(*) c FROM v12_raw_message")[0]["c"] == 2
        assert db.query("SELECT COUNT(*) c FROM v12_trajectory")[0]["c"] == 1
        assert db.query("SELECT COUNT(*) c FROM v12_video")[0]["c"] == 2

    def test_dt2_field_mapping(self, client):
        from app.ingest.identity import pseudonymize_athlete
        out = _post(client, _examples()[:4])
        r2 = next(r for r in out["results"] if r["messageId"] == "msg_002")
        aid = pseudonymize_athlete("spid123")
        assert r2["athlete_id"] == aid and "spid123" not in aid
        rows = _db().query("SELECT * FROM shot_fact WHERE shot_id=?", ("1963169497552654336",))
        assert len(rows) == 1
        s = dict(rows[0])
        assert s["athlete_id"] == aid
        assert s["session_id"] == f"{aid}_20250909_v01"            # 10:18Z = 本地 18:18 同日
        assert s["shot_time_utc"] == "2025-09-09T10:18:44.620Z"    # = releaseTime（锚点）
        assert s["release_time_utc"] == "2025-09-09T10:18:44.620Z"
        assert s["hit_time_utc"] == "2025-09-09T10:18:45.840Z"
        assert s["flight_time_ms"] == 1220
        assert s["score_id"] == "1963169497552654336" and isinstance(s["shot_id"], str)
        assert s["lane"] == "lane-01"
        assert s["score"] == 10.0 and s["hit"] == 1 and s["inner_ten"] == 0
        assert s["x_mm"] == pytest.approx(45.0) and s["y_mm"] == pytest.approx(-20.0)   # cm → mm
        assert s["hr"] == 92 and s["wind_speed"] == 3.2 and s["wind_dir_deg"] == 70
        assert s["shooting_mode"] == 1 and s["bow_type"] == "反曲弓"
        assert s["video_ref"] == "/videos/s1_shot1_top.mp4"
        prof = _db().get_profile(aid)
        assert prof is not None and prof["identity_id"] is None and "spid" not in (prof["name"] or "")

    def test_compound_bow_mapping(self, client):
        _post(client, [make_dt2(1, bowType="compound", targetType="80cm-compound")])
        assert _db().query("SELECT bow_type FROM shot_fact")[0]["bow_type"] == "复合弓"

    def test_duplicate_message_id(self, client):
        first = _post(client, _examples())
        again = _post(client, _examples())
        for a, b in zip(first["results"], again["results"]):
            if a["status"] == "accepted":
                assert b["status"] == "duplicate", b
            else:
                assert b["status"] == a["status"]
        assert again["summary"]["sessions_rebuilt"] == 0
        assert _db().query("SELECT COUNT(*) c FROM shot_fact")[0]["c"] == 1

    def test_duplicate_within_batch(self, client):
        m = make_dt2(1)
        out = _post(client, [m, m])
        assert [r["status"] for r in out["results"]] == ["accepted", "duplicate"]

    def test_same_shot_new_message_id_not_overwritten(self, client):
        _post(client, [make_dt2(1, score=9)])
        out = _post(client, [make_dt2(1, score=3, mid_prefix="resend")])
        assert out["results"][0]["status"] == "duplicate"
        assert "shotId" in out["results"][0]["reason"]
        assert _db().query("SELECT score FROM shot_fact")[0]["score"] == 9.0

    def test_invalid_not_stored_and_can_be_resent(self, client):
        bad = make_dt2(1)
        bad["data"]["releaseTime"] = "2026-09-07T09:00:30.000+08:00"
        out = _post(client, [bad])
        res = out["results"][0]
        assert res["status"] == "invalid" and res["errors"] and res["messageId"] == bad["messageId"]
        assert _db().query("SELECT COUNT(*) c FROM ingest_messages")[0]["c"] == 0
        assert _db().query("SELECT COUNT(*) c FROM shot_fact")[0]["c"] == 0
        out2 = _post(client, [make_dt2(1)])     # 修正后同 messageId 重发 → 受理
        assert out2["results"][0]["status"] == "accepted"

    def test_mixed_batch_isolated(self, client):
        bad = make_dt2(2)
        bad["data"]["score"] = 0
        bad["data"]["scoreId"] = None
        out = _post(client, [make_dt2(1), bad, "not-an-object", make_dt2(3)])
        assert [r["status"] for r in out["results"]] == ["accepted", "invalid", "invalid", "accepted"]
        assert _db().query("SELECT COUNT(*) c FROM shot_fact")[0]["c"] == 2

    def test_single_object_body(self, client):
        out = _post(client, make_dt2(1))
        assert out["summary"]["total"] == 1 and out["results"][0]["status"] == "accepted"

    def test_bad_bodies(self, client):
        assert client.post(URL, json=5).status_code == 400
        assert client.post(URL, json=[]).status_code == 400
        assert client.post(URL, json=[make_dt2(i) for i in range(501)]).status_code == 413

    def test_mock_dt5_dt6_stored_raw(self, client):
        out = _post(client, _mock_messages())
        sts = [r["status"] for r in out["results"]]
        # 全量 dt5/dt6 与演示版 jsonc 共用同一 messageId（msg_..._0001/0002）→ 后两条按 §4.3 去重
        assert sts == ["accepted", "accepted", "duplicate", "duplicate"]
        assert _db().query("SELECT COUNT(*) c FROM shot_fact")[0]["c"] == 0  # 不参与计算

    def test_flight_time_warning(self, client):
        out = _post(client, [make_dt2(1, flightTimeMs=900)])
        assert out["results"][0]["status"] == "accepted"
        assert any("flightTimeMs" in w for w in out["results"][0]["warnings"])


# ---------------------------------------------------------------- 缺测哨兵

class TestMissingMeasurement:
    def test_dt2_sentinels_to_null(self, client):
        _post(client, [make_dt2(1, heartRate=0, windSpeed=-1, windDirection=-1, offsetM=-1)])
        s = dict(_db().query("SELECT * FROM shot_fact")[0])
        assert s["hr"] is None and s["wind_speed"] is None and s["wind_dir_deg"] is None
        v = dict(_db().query("SELECT * FROM v12_shot")[0])
        assert v["offset_m"] is None
        dim = dict(_db().query("SELECT * FROM session_dim")[0])
        assert dim["avg_wind"] is None

    def test_zero_wind_is_real_calm(self, client):
        _post(client, [make_dt2(1, windSpeed=0, windDirection=0)])
        s = dict(_db().query("SELECT * FROM shot_fact")[0])
        assert s["wind_speed"] == 0.0 and s["wind_dir_deg"] == 0.0

    def test_realtime_sentinels(self, client):
        hr = _by_dt(1)
        hr["data"]["heartRate"] = 0
        wind = _by_dt(4)
        wind["data"]["windSpeed"] = -1
        wind["data"]["windDirection"] = -1
        _post(client, [hr, wind])
        rows = {r["data_type"]: dict(r) for r in _db().query("SELECT * FROM v12_sample")}
        assert rows[1]["hr"] is None and rows[1]["rr_intervals_ms"] is None and rows[1]["rmssd_ms"] is None
        assert rows[4]["wind_speed"] is None and rows[4]["wind_dir_deg"] is None

    def test_snapshot_missing_falls_back_to_realtime_by_shot_id(self, client):
        m = make_dt2(1, heartRate=0, windSpeed=-1, windDirection=-1, offsetM=-1)
        sid, rel = m["data"]["shotId"], m["data"]["releaseTime"]
        _post(client, [m])
        assert _db().query("SELECT hr FROM shot_fact")[0]["hr"] is None
        hr = _by_dt(1)
        hr["messageId"] = "late-hr"
        hr["data"].update(shotId=sid, timestamp=rel, heartRate=101)
        wind = _by_dt(4)
        wind["messageId"] = "late-wind"
        wind["data"].update(shotId=sid, timestamp=rel, windSpeed=2.2, windDirection=45)
        out = _post(client, [hr, wind])        # 实时流晚到：凭 shotId 触发该日重建
        assert out["summary"]["sessions_rebuilt"] == 1
        s = dict(_db().query("SELECT * FROM shot_fact")[0])
        assert s["hr"] == 101 and s["wind_speed"] == 2.2 and s["wind_dir_deg"] == 45

    def test_wind_band_of_negative_never_throws(self, engine_env):
        from app.metrics.environment import wind_band_avg_scores, wind_band_counts, wind_band_of
        assert wind_band_of(-1) is None and wind_band_of(-0.1) is None and wind_band_of(None) is None
        shots = [{"wind_speed": -1, "score": 9.0}, {"wind_speed": 0.5, "score": 8.0}]
        assert wind_band_counts(shots) == {0: 1}
        assert wind_band_avg_scores(shots) == {0: 8.0}

    def test_align_sanitizes_any_source(self, engine_env):
        from app.ingest.align import align_session
        from app.ingest.base import SessionRaw, ShotRaw
        sh = ShotRaw(athlete_id="A", session_id="S", shot_seq=1, score=9.0, hit=True, hr=0,
                     wind_speed=-1.0, wind_dir_deg=-1.0, shot_time_utc="2026-09-07T01:00:00.000Z")
        dim, rows = align_session(SessionRaw(session_id="S", athlete_id="A", shots=[sh]))
        assert rows[0]["hr"] is None and rows[0]["wind_speed"] is None and rows[0]["wind_dir_deg"] is None
        assert dim["avg_wind"] is None


# ---------------------------------------------------------------- 场次重建 + 端到端报告

class TestSessionsAndReports:
    def test_out_of_order_batches_one_session(self, client):
        from app.ingest.identity import pseudonymize_athlete
        aid = pseudonymize_athlete("spid-test-a")
        _post(client, [make_dt2(i) for i in range(10, 20)])
        _post(client, [make_dt2(i) for i in range(0, 10)])        # 较早的箭后到
        sess = _db().query("SELECT * FROM session_dim")
        assert [s["session_id"] for s in sess] == [f"{aid}_20260907_v01"]
        assert sess[0]["shot_count"] == 20
        shots = _db().shots_of_session(f"{aid}_20260907_v01")
        assert [s["shot_seq"] for s in shots] == list(range(1, 21))
        times = [s["shot_time_utc"] for s in shots]
        assert times == sorted(times)

    def test_gap_splits_sessions_and_athletes_isolated(self, client):
        a = [make_dt2(i, athlete="spid-a") for i in range(5)]
        b = [make_dt2(i, athlete="spid-b", shot_base=1963169497552800000, step_s=31) for i in range(5)]
        late = [make_dt2(i, athlete="spid-a", base="2026-09-07T05:00:00.000Z",
                         shot_base=1963169497552900000) for i in range(3)]
        interleaved = [m for pair in zip(a, b) for m in pair] + late
        _post(client, interleaved)
        rows = _db().query("SELECT athlete_id, session_id, shot_count FROM session_dim ORDER BY session_id")
        counts = sorted(r["shot_count"] for r in rows)
        assert counts == [3, 5, 5]
        assert len({r["athlete_id"] for r in rows}) == 2

    def test_e2e_daily_and_weekly_report_from_v12(self, client):
        from app.ingest.identity import pseudonymize_athlete
        aid = pseudonymize_athlete("spid-test-a")
        msgs = [make_dt2(i, score=8 + (i % 3), hr=85 + (i % 5), wind=0.5 + (i % 4) * 0.5) for i in range(30)]
        msgs[3]["data"].update(heartRate=0, windSpeed=-1, windDirection=-1, offsetM=-1)   # 混入缺测
        out = _post(client, msgs)
        assert out["summary"]["accepted"] == 30
        sid = f"{aid}_20260907_v01"
        r = client.post(f"/api/v1/athletes/{aid}/reports/daily", params={"session_id": sid})
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["athlete"]["id"] == aid and rep["sections"]
        assert "样本不足" not in json.dumps(rep["sections"], ensure_ascii=False)   # 30 箭 ≥ 日报门槛 20
        w = client.post(f"/api/v1/athletes/{aid}/reports/weekly", params={"week": "2026-W37"})
        assert w.status_code == 200, w.text
        snap = _db().latest_snapshot(aid, "rolling")
        assert snap is not None and snap["n_shots"] == 30
        # 档案接口不暴露身份字段
        p = client.get(f"/api/v1/athletes/{aid}/profile").json()
        assert "identity_id" not in p
        g = client.get(f"/api/v1/athletes/{aid}").json()
        assert "identity_id" not in (g["profile"] or {})

    def test_rebuild_invalidates_daily_cache(self, client):
        from app.ingest.identity import pseudonymize_athlete
        aid = pseudonymize_athlete("spid-test-a")
        _post(client, [make_dt2(i) for i in range(25)])
        sid = f"{aid}_20260907_v01"
        client.post(f"/api/v1/athletes/{aid}/reports/daily", params={"session_id": sid})
        assert _db().query("SELECT COUNT(*) c FROM report_cache WHERE window_key=?", (f"daily:{sid}",))[0]["c"] == 1
        _post(client, [make_dt2(25)])
        assert _db().query("SELECT COUNT(*) c FROM report_cache WHERE window_key=?", (f"daily:{sid}",))[0]["c"] == 0


def test_duplicate_message_id_with_different_payload_flagged(client):
    m = make_dt2(1)
    _post(client, [m])
    changed = copy.deepcopy(m)
    changed["data"]["score"] = 5
    res = _post(client, [changed])["results"][0]
    assert res["status"] == "duplicate" and res["payload_differs"] is True
    assert _db().query("SELECT score FROM shot_fact")[0]["score"] == 9.0
