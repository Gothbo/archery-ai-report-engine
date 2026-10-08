# -*- coding: utf-8 -*-
"""PR #4：引擎结论（verdicts / 风档差 / 和自己比 / 平台期修正）+ metrics。"""
import json

import pytest

from app.config import get_config
from app.reports.conclusions import compute_wind_gap
from app.reports.generator import generate_report
from tests.conftest import ATHLETE, seed_anchor, seed_session

A = "777"


def _seed_weeks(db, scores_by_week=None, *, distance=70):
    """锚点（W31）+ W32/W33/W34 各 2 场 × 15 箭，默认全部与锚点持平。"""
    db.upsert_profile({"athlete_id": A, "name": "测试"})
    seed_session(db, A, "ANC", "2026-07-28T01:00:00.000Z", [9.0] * 30)
    seed_anchor(db, A, "ANC")
    days = {"2026-W32": ("2026-08-03", "2026-08-05"), "2026-W33": ("2026-08-10", "2026-08-12"),
            "2026-W34": ("2026-08-17", "2026-08-19")}
    for wk, (d1, d2) in days.items():
        sc = (scores_by_week or {}).get(wk, [9.1] * 15)
        seed_session(db, A, f"{wk}-a", f"{d1}T01:00:00.000Z", sc, distance_m=distance)
        seed_session(db, A, f"{wk}-b", f"{d2}T01:00:00.000Z", sc, distance_m=distance)


def _rep(db, week, force=False, view="coach"):
    return generate_report(db, A, "weekly", week, view=view, force=force)


class TestMetricsAndVerdicts:
    def test_metrics_block(self, mock_db):
        rep = generate_report(mock_db, ATHLETE, "weekly", "2026-W33", view="coach")
        m = rep["metrics"]
        assert set(m) == {"n_shots", "avg_score", "inner10_rate", "far_miss_rate", "hit_rate",
                          "total_score", "mcr_t", "hr_volatility", "dispersion_mm"}
        assert m["n_shots"] == 90
        assert f"平均环 {m['avg_score']}" in rep["sections"][0]["content"][0]  # 与文案同源
        assert rep["conclusions"]["schema_version"] == 1

    def test_no_anchor(self, mock_db):
        v = generate_report(mock_db, ATHLETE, "weekly", "2026-W33", view="coach")["conclusions"]["verdicts"]
        assert set(v) == {"avgScore", "mcrT", "dispersionMm", "hrVolatility"}
        assert all(x["verdict"] is None and x["reason"] == "no_anchor" and x["anchor"] is None for x in v.values())

    def test_sample_insufficient(self, mock_db):
        sid = mock_db.query("SELECT session_id FROM session_dim ORDER BY session_time_utc LIMIT 1")[0]["session_id"]
        seed_anchor(mock_db, ATHLETE, sid)
        v = generate_report(mock_db, ATHLETE, "yearly", "2026", view="coach")["conclusions"]["verdicts"]
        assert all(x["verdict"] is None and x["reason"] == "sample_insufficient" for x in v.values())

    def test_judged_with_signed_delta(self, mock_db):
        sid = mock_db.query("SELECT session_id FROM session_dim ORDER BY session_time_utc LIMIT 1")[0]["session_id"]
        seed_anchor(mock_db, ATHLETE, sid)
        rep = generate_report(mock_db, ATHLETE, "weekly", "2026-W35", view="coach")
        v = rep["conclusions"]["verdicts"]
        a = v["avgScore"]
        assert a["delta"] == round(a["value"] - a["anchor"], 3)
        assert a["mdc"] == get_config().mdc["avgScore"].threshold
        if abs(a["delta"]) >= a["mdc"]:
            assert a["reason"] == "above_mdc" and a["verdict"] == ("progress" if a["delta"] > 0 else "regression")
        d = v["dispersionMm"]  # 越小越好：delta < 0 且超过 MDC → progress
        if d["delta"] is not None and d["delta"] <= -d["mdc"]:
            assert d["verdict"] == "progress"
        assert v["hrVolatility"]["reason"] == "mdc_pending"  # 阈值待共识
        # 判定与 sections 文案一致
        assert ("进步" in json.dumps(rep["sections"], ensure_ascii=False)) == (a["verdict"] == "progress")

    def test_mdc_pending_when_degraded(self, degraded_env):
        from app.store.database import Database
        db = Database(degraded_env[1])
        _seed_weeks(db)
        v = _rep(db, "2026-W32")["conclusions"]["verdicts"]
        assert v["avgScore"]["verdict"] is None and v["avgScore"]["reason"] == "mdc_pending"
        assert v["dispersionMm"]["reason"] == "missing_data"  # 种子数据没有坐标
        db.close()

    def test_not_comparable(self, db):
        _seed_weeks(db, distance=50)  # 锚点 70 m，窗口 50 m
        v = _rep(db, "2026-W32")["conclusions"]["verdicts"]
        assert v["avgScore"]["verdict"] is None and v["avgScore"]["reason"] == "not_comparable"
        assert v["avgScore"]["delta"] == pytest.approx(0.1)


class TestWindGap:
    def _shots(self, low, high, low_score=9.5, high_score=9.0):
        return ([{"score": low_score, "wind_speed": 1.0}] * low +
                [{"score": high_score, "wind_speed": 2.6}] * high)

    def test_ok_triggered_sign(self, engine_env):
        w = compute_wind_gap(get_config(), self._shots(10, 6))
        assert w["status"] == "ok" and w["triggered"] is True
        assert w["gap"] == 0.5  # 低风 − 高风，正数 = 风拉低成绩
        assert w["low"] == {"n_shots": 10, "avg_score": 9.5}
        assert w["high"] == {"n_shots": 6, "avg_score": 9.0}
        assert w["split_mps"] == 2.0 and w["min_shots"] == 5 and w["threshold"] == 0.3
        assert w["provisional"] is True

    def test_below_threshold_not_triggered(self, engine_env):
        w = compute_wind_gap(get_config(), self._shots(10, 6, 9.2, 9.0))
        assert w["status"] == "ok" and w["gap"] == 0.2 and w["triggered"] is False

    def test_boundary_threshold_triggers(self, engine_env):
        w = compute_wind_gap(get_config(), self._shots(10, 6, 9.3, 9.0))
        assert w["gap"] == 0.3 and w["triggered"] is True

    def test_negative_gap(self, engine_env):
        w = compute_wind_gap(get_config(), self._shots(10, 6, 9.0, 9.5))
        assert w["gap"] == -0.5 and w["triggered"] is False

    def test_insufficient_high_side(self, engine_env):
        w = compute_wind_gap(get_config(), self._shots(20, 4))
        assert w["status"] == "insufficient_sample" and w["gap"] is None and w["triggered"] is False
        assert w["high"]["n_shots"] == 4

    def test_no_wind_and_sentinels_ignored(self, engine_env):
        shots = [{"score": 9.0, "wind_speed": None}, {"score": 9.0, "wind_speed": -1},
                 {"score": 9.0, "wind_speed": float("nan")}]
        w = compute_wind_gap(get_config(), shots)
        assert w["status"] == "no_wind_data" and w["low"] == {"n_shots": 0, "avg_score": None}

    def test_split_is_exact_boundary(self, engine_env):
        shots = [{"score": 9.0, "wind_speed": 2.0}] * 5 + [{"score": 9.5, "wind_speed": 1.99}] * 5
        w = compute_wind_gap(get_config(), shots)
        assert w["high"]["n_shots"] == 5 and w["low"]["n_shots"] == 5  # 2.0 归高风（左闭右开）

    def test_in_report(self, mock_db):
        rep = generate_report(mock_db, ATHLETE, "weekly", "2026-W35", view="coach")
        w = rep["conclusions"]["wind_gap"]
        assert w["low"]["n_shots"] + w["high"]["n_shots"] <= rep["metrics"]["n_shots"]
        assert w["status"] in ("ok", "insufficient_sample", "no_wind_data")


class TestSelfCompare:
    def test_daily_previous_session(self, db):
        seed_session(db, A, "D1", "2026-08-03T01:00:00.000Z", [9.0] * 10)
        seed_session(db, A, "D2", "2026-08-04T01:00:00.000Z", [9.5] * 10)
        s = generate_report(db, A, "daily", "daily:D2", session_id="D2")["conclusions"]["self_compare"]
        assert s["status"] == "ok" and s["direction"] == "higher" and s["delta"] == 0.5
        assert s["previous"]["window_key"] == "daily:D1" and s["previous"]["avg_score"] == 9.0
        assert s["previous"]["started_at_utc"] == "2026-08-03T01:00:00.000Z"
        assert s["previous"]["bow_type"] == "反曲弓" and s["previous"]["distance_m"] == 70
        assert s["comparable"] is True and s["current_avg_score"] == 9.5

    def test_daily_first_session_no_previous(self, db):
        seed_session(db, A, "D1", "2026-08-03T01:00:00.000Z", [9.0] * 10)
        s = generate_report(db, A, "daily", "daily:D1", session_id="D1")["conclusions"]["self_compare"]
        assert s["status"] == "no_previous" and s["previous"] is None and s["delta"] is None

    def test_daily_not_comparable_distance(self, db):
        seed_session(db, A, "D1", "2026-08-03T01:00:00.000Z", [9.0] * 10, distance_m=50)
        seed_session(db, A, "D2", "2026-08-04T01:00:00.000Z", [9.0] * 10)
        s = generate_report(db, A, "daily", "daily:D2", session_id="D2")["conclusions"]["self_compare"]
        assert s["comparable"] is False and s["direction"] == "same" and s["delta"] == 0

    def test_daily_other_athlete_not_used(self, db):
        seed_session(db, "OTHER", "X1", "2026-08-03T01:00:00.000Z", [5.0] * 10)
        seed_session(db, A, "D2", "2026-08-04T01:00:00.000Z", [9.0] * 10)
        s = generate_report(db, A, "daily", "daily:D2", session_id="D2")["conclusions"]["self_compare"]
        assert s["status"] == "no_previous"

    def test_weekly_previous_window(self, db):
        _seed_weeks(db, {"2026-W32": [9.0] * 15, "2026-W33": [8.8] * 15})
        s = _rep(db, "2026-W33")["conclusions"]["self_compare"]
        assert s["status"] == "ok" and s["previous"]["window_key"] == "2026-W32"
        assert s["previous"]["n_shots"] == 30 and s["delta"] == -0.2 and s["direction"] == "lower"
        assert s["comparable"] is None and s["previous"]["started_at_utc"] is None

    def test_weekly_previous_window_empty(self, db):
        _seed_weeks(db)
        s = _rep(db, "2026-W32")["conclusions"]["self_compare"]  # W31 只有锚点场次
        assert s["status"] == "ok" and s["previous"]["window_key"] == "2026-W31"
        s2 = generate_report(db, A, "weekly", "2026-W36")["conclusions"]["self_compare"]
        assert s2["status"] == "no_data"
        s3 = generate_report(db, A, "weekly", "2026-W30")["conclusions"]["self_compare"]
        assert s3["status"] == "no_data"

    def test_weekly_gap_week_no_previous(self, db):
        _seed_weeks(db)
        seed_session(db, A, "W36a", "2026-08-31T01:00:00.000Z", [9.0] * 15)
        s = generate_report(db, A, "weekly", "2026-W36")["conclusions"]["self_compare"]
        assert s["status"] == "no_previous"  # W35 没有数据，不往前找

    def test_monthly_cross_year(self, db):
        seed_session(db, A, "Y1", "2025-12-10T01:00:00.000Z", [9.0] * 10)
        seed_session(db, A, "Y2", "2026-01-10T01:00:00.000Z", [9.4] * 10)
        s = generate_report(db, A, "monthly", "2026-01")["conclusions"]["self_compare"]
        assert s["previous"]["window_key"] == "2025-12" and s["delta"] == 0.4


class TestPlateau:
    def test_three_steady_periods_trigger(self, db):
        _seed_weeks(db)
        assert _rep(db, "2026-W32")["conclusions"]["plateau"]["status"] == "insufficient_history"
        p2 = _rep(db, "2026-W33")["conclusions"]["plateau"]
        assert p2["triggered"] is False and len(p2["recent"]) == 2
        rep3 = _rep(db, "2026-W34")
        p3 = rep3["conclusions"]["plateau"]
        assert p3["status"] == "ok" and p3["triggered"] is True
        assert [r["window_key"] for r in p3["recent"]] == ["2026-W32", "2026-W33", "2026-W34"]
        assert p3["recent"][-1]["report_id"] == rep3["report_id"]
        assert p3["metric"] == "avgScore" and p3["periods"] == 3
        assert any(s["key"] == "plateau" for s in rep3["sections"])
        keys = [r["conclusion_key"] for r in db.query(
            "SELECT conclusion_key FROM report_memories WHERE report_id=?", (rep3["report_id"],))]
        assert "plateau" in keys

    def test_bug_regression_one_report_three_metrics_not_plateau(self, db):
        """旧逻辑：上一份报告写了 ≥3 行 steady（多个指标）→ 第 2 份报告就误报平台期。"""
        _seed_weeks(db)
        r1 = _rep(db, "2026-W32")
        # 给第一份报告补 2 行 steady 记忆，模拟「一份报告 3 个指标都平稳」
        from app.memory.memories import record_conclusion
        for _ in range(2):
            record_conclusion(db, athlete_id=A, report_id=r1["report_id"], granularity="weekly",
                              conclusion_key="steady", conclusion="x", judge_basis=None,
                              delta_value=0.0, evidence="")
        n = db.query("SELECT COUNT(*) c FROM report_memories WHERE report_id=? AND conclusion_key='steady'",
                     (r1["report_id"],))[0]["c"]
        assert n >= 3
        r2 = _rep(db, "2026-W33")
        assert r2["conclusions"]["plateau"]["triggered"] is False
        assert not any(s["key"] == "plateau" for s in r2["sections"])

    def test_non_steady_breaks_plateau(self, db):
        _seed_weeks(db, {"2026-W33": [9.6] * 15})  # W33 超过 MDC → progress
        _rep(db, "2026-W32"); _rep(db, "2026-W33")
        p = _rep(db, "2026-W34")["conclusions"]["plateau"]
        assert p["status"] == "ok" and p["triggered"] is False
        assert [r["verdict"] for r in p["recent"]] == ["steady", "progress", "steady"]

    def test_window_order_not_generation_order(self, db):
        _seed_weeks(db)
        p_first = _rep(db, "2026-W34")["conclusions"]["plateau"]  # 先生成最新一周
        assert p_first["status"] == "insufficient_history"
        p_old = _rep(db, "2026-W32")["conclusions"]["plateau"]
        assert [r["window_key"] for r in p_old["recent"]] == ["2026-W32"]  # 不使用更晚的窗口
        _rep(db, "2026-W33")
        p = _rep(db, "2026-W34", force=True)["conclusions"]["plateau"]
        assert [r["window_key"] for r in p["recent"]] == ["2026-W32", "2026-W33", "2026-W34"]
        assert p["triggered"] is True

    def test_regenerate_does_not_duplicate(self, db):
        _seed_weeks(db)
        _rep(db, "2026-W32"); _rep(db, "2026-W33", force=True); _rep(db, "2026-W33", force=True)
        p = _rep(db, "2026-W34")["conclusions"]["plateau"]
        assert [r["window_key"] for r in p["recent"]] == ["2026-W32", "2026-W33", "2026-W34"]

    def test_legacy_reports_without_body_ignored(self, db):
        _seed_weeks(db)
        db.put_cached_report("OLD1", A, "weekly", "2026-W32", "test-consensus-v1")  # 无正文
        _rep(db, "2026-W33")
        p = _rep(db, "2026-W34")["conclusions"]["plateau"]
        assert p["status"] == "insufficient_history"
        assert "OLD1" not in [r["report_id"] for r in p["recent"]]

    def test_daily_plateau_by_session_time(self, db):
        seed_session(db, A, "ANC", "2026-07-28T01:00:00.000Z", [9.0] * 30)
        seed_anchor(db, A, "ANC")
        for i, sid in enumerate(["Z3", "Z1", "Z2"]):  # 场次号顺序与时间顺序不同
            seed_session(db, A, sid, f"2026-08-0{[5, 3, 4][i]}T01:00:00.000Z", [9.1] * 25)
        for sid in ("Z1", "Z2", "Z3"):
            rep = generate_report(db, A, "daily", f"daily:{sid}", session_id=sid)
        p = rep["conclusions"]["plateau"]  # Z3 时间最晚（08-05）
        assert [r["window_key"] for r in p["recent"]] == ["daily:Z1", "daily:Z2", "daily:Z3"]
        assert p["triggered"] is True

    def test_config_periods(self, db, engine_env, tmp_path):
        import app.config as cfgmod
        p = tmp_path / "config.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg["conclusions"] = {"plateau_periods": 2}
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        cfgmod.get_config.cache_clear()
        _seed_weeks(db)
        _rep(db, "2026-W32")
        pl = _rep(db, "2026-W33")["conclusions"]["plateau"]
        assert pl["periods"] == 2 and pl["triggered"] is True


class TestStoredConclusions:
    def test_cache_and_content_same_conclusions(self, mock_db):
        r1 = generate_report(mock_db, ATHLETE, "weekly", "2026-W33", view="coach")
        r2 = generate_report(mock_db, ATHLETE, "weekly", "2026-W33", view="athlete")
        assert r2["cached"] is True
        assert r2["conclusions"] == r1["conclusions"] and r2["metrics"] == r1["metrics"]
