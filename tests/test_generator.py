# -*- coding: utf-8 -*-
"""M4：ingest 幂等 + 报告生成器集成测试（mock 数据全链路）。"""
import pytest

from app.config import get_config
from app.reports.generator import build_report, generate_report
from app.reports.window import window_of_shot
from app.store.database import Database
from tests.conftest import ATHLETE, seed_anchor, seed_session

CANONICAL_SIX = ["window_score", "level", "data_integrity", "wind_bands", "sample_gate", "auxiliary"]


def _keys(rep: dict) -> list[str]:
    return [s["key"] for s in rep["sections"]]


def _texts(rep: dict) -> str:
    return " ".join(" ".join(str(c) for c in s["content"]) for s in rep["sections"])


class TestSixSectionSkeleton:
    """T1 验收：build_report() 恒输出固定六段骨架，顺序即优先级；缺失即标注。"""

    def test_canonical_six_keys_and_order(self, engine_env):
        """正常窗口（含风速）：六段 key 恒出现且顺序固定（score→level→integrity→wind→gate→aux）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, wind=[0.8] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        assert _keys(draft.report) == CANONICAL_SIX
        db.close()

    def test_no_notes_yields_explicit_empty_auxiliary(self, engine_env):
        """无备注：auxiliary 照常出现，内容为显式空说明。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        aux = [s for s in draft.report["sections"] if s["key"] == "auxiliary"][0]
        assert aux["content"] and "暂无" in aux["content"][0]
        db.close()

    def test_no_wind_merges_into_data_integrity(self, engine_env):
        """无风速数据：wind_bands 不单独成段，缺失并入 data_integrity 显式说明。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, wind=None)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        keys = _keys(draft.report)
        assert "wind_bands" not in keys
        integrity = [s for s in draft.report["sections"] if s["key"] == "data_integrity"][0]
        assert "无风速数据" in integrity["content"][0]
        db.close()

    def test_with_wind_data_emits_wind_bands(self, engine_env):
        """有风速数据：wind_bands 独立成段，data_integrity 不重复标注缺失。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30,
                     wind=[0.5 + (i % 5) * 0.3 for i in range(30)])
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        keys = _keys(draft.report)
        assert "wind_bands" in keys
        integrity = [s for s in draft.report["sections"] if s["key"] == "data_integrity"][0]
        assert "无风速数据" not in integrity["content"][0]
        db.close()

    def test_sample_gate_says_ok_when_met(self, engine_env):
        """样本达标：sample_gate 显式出现「样本达标」。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        gate = [s for s in draft.report["sections"] if s["key"] == "sample_gate"][0]
        assert "样本达标" in gate["content"][0]
        db.close()

    def test_sample_gate_warns_when_below(self, engine_env):
        """样本不达标：sample_gate 出提示（仍不出判定词）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 5)  # 5 < 20
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        gate = [s for s in draft.report["sections"] if s["key"] == "sample_gate"][0]
        assert "样本不足" in gate["content"][0]
        db.close()

    def test_empty_window_is_explicitly_flagged(self, engine_env):
        """窗口无任何箭：各段不静默消失，data_integrity 显式说明空窗口。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        db.upsert_profile({"athlete_id": ATHLETE, "name": "测试"})
        draft = build_report(db, ATHLETE, "weekly", "2026-W33", view="coach")
        assert _keys(draft.report) == [k for k in CANONICAL_SIX if k != "wind_bands"]
        texts = _texts(draft.report)
        assert "空窗口" in texts
        db.close()

    def test_build_report_is_side_effect_free(self, engine_env):
        """build_report() 不写库：无缓存行、无结论记忆。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        assert db.cached_reports_of_athlete(ATHLETE) == []
        assert db.list_memories(ATHLETE) == []
        assert draft.conclusions == []
        db.close()

    def test_judgement_landed_in_level_section(self, engine_env):
        """有锚点且 MDC 判定存在：判定结论落入 level 段，不再以 avg_vs_anchor 等散段出现。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.5] * 30, wind=[1.0] * 30)
        seed_anchor(db, ATHLETE, "S-A")
        seed_session(db, ATHLETE, "S-B", "2026-08-05T01:00:00.000Z", [9.8] * 30, wind=[1.0] * 30)
        draft = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach")
        keys = _keys(draft.report)
        assert keys == CANONICAL_SIX
        assert not {"avg_vs_anchor", "mcr_vs_anchor", "dispersion_vs_anchor"} & set(keys)
        level = [s for s in draft.report["sections"] if s["key"] == "level"][0]
        assert "较锚点" in level["content"][0] or "与锚点" in level["content"][0]
        db.close()


class TestIngestIdempotency:
    def test_reimport_skips_shots(self, mock_db):
        n1 = mock_db._query("SELECT COUNT(*) c FROM shot_fact")[0]["c"]
        # 重新导入同一 mock 源 → 幂等（0 新增）
        from app.ingest import ingest_source
        from app.ingest.mock_source import MockSource
        from app.memory.baseline import refresh_rolling_after_import
        stats = ingest_source(mock_db, MockSource(), on_imported=refresh_rolling_after_import)
        n2 = mock_db._query("SELECT COUNT(*) c FROM shot_fact")[0]["c"]
        assert stats["inserted_shots"] == 0 and stats["skipped_shots"] == 390
        assert n1 == n2

    def test_import_creates_rolling_baseline(self, mock_db):
        snap = mock_db.latest_snapshot(ATHLETE, "rolling")
        assert snap is not None
        assert snap["n_shots"] == 288  # 9B 口径：最近 288 支记分箭


class TestGeneratorMock:
    def test_daily_report(self, mock_db):
        sid = mock_db.sessions_of_athlete(ATHLETE)[0]["session_id"]
        rep = generate_report(mock_db, ATHLETE, "daily", f"daily:{sid}", session_id=sid, view="athlete")
        keys = {s["key"] for s in rep["sections"]}
        assert "window_score" in keys
        assert rep["granularity"] == "daily"
        assert rep["athlete"]["name"] == "张明"

    def test_weekly_monthly_quarterly_windows(self, mock_db):
        # 全 13 场：W32(4场) W33(3场) W34(4场) W35(2场)；月 2026-08 全量
        for week in ("2026-W32", "2026-W33", "2026-W34", "2026-W35"):
            rep = generate_report(mock_db, ATHLETE, "weekly", week, view="coach", force=True)
            scores = [s for s in rep["sections"] if s["key"] == "window_score"]
            assert scores, f"{week} 无成绩段"
        rep_m = generate_report(mock_db, ATHLETE, "monthly", "2026-08", view="coach", force=True)
        assert any(s["key"] == "window_score" for s in rep_m["sections"])

    def test_cache_hit_and_refresh(self, mock_db):
        r1 = generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach")
        r2 = generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach")
        assert r1["cached"] is not True and r2["cached"] is True
        assert r1["report_id"] == r2["report_id"]
        r3 = generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach", force=True)
        assert r3["cached"] is not True
        assert r3["report_id"] != r1["report_id"]  # 强制重生成换 id

    def test_year_gate_blocks(self, mock_db):
        rep = generate_report(mock_db, ATHLETE, "yearly", "2026", view="coach", force=True)
        keys = {s["key"] for s in rep["sections"]}
        assert "sample_gate" in keys  # 390 < 1040 门槛 → 不出判定词

    def test_anchor_mdc_judgement_and_persistence(self, mock_db):
        sid = mock_db.sessions_of_athlete(ATHLETE)[0]["session_id"]
        seed_anchor(mock_db, ATHLETE, sid)
        rep = generate_report(mock_db, ATHLETE, "weekly", "2026-W33", view="coach", force=True)
        keys = {s["key"] for s in rep["sections"]}
        assert "level" in keys  # 有锚点 → level 段出 MDC 判定
        mems = mock_db.list_memories(ATHLETE)
        assert mems, "结论应回写记忆"
        assert any(m["conclusion_type"] == "judgement" for m in mems)

    def test_degraded_report_when_no_mdc(self, degraded_env):
        from app.store.database import Database
        from app.ingest import ingest_source
        from app.ingest.mock_source import MockSource
        db = Database(degraded_env[1])
        ingest_source(db, MockSource())
        sid = db.sessions_of_athlete(ATHLETE)[0]["session_id"]
        seed_anchor(db, ATHLETE, sid)
        rep = generate_report(db, ATHLETE, "weekly", "2026-W33", view="coach", force=True)
        texts = " ".join(s["content"][0] for s in rep["sections"])
        assert "未判定" in texts or "MDC 口径待专家共识" in texts  # 降级：只描述不判定
        db.close()


class TestNotesVisibility:
    def test_view_filters_note_types(self, mock_db):
        from app.memory.notes import add_note, list_notes_for_view
        add_note(mock_db, ATHLETE, "injury", "右肩轻微酸痛", "athlete", ATHLETE)
        add_note(mock_db, ATHLETE, "coach_note", "撒放偏快", "coach", ATHLETE)
        add_note(mock_db, ATHLETE, "goal", "冲击 9.8 均环", "athlete", ATHLETE)
        athlete_notes = list_notes_for_view(mock_db, ATHLETE, "athlete")
        coach_notes = list_notes_for_view(mock_db, ATHLETE, "coach")
        assert {n["note_type"] for n in athlete_notes} == {"injury", "goal"}  # 教练观察默认隐藏
        assert {n["note_type"] for n in coach_notes} == {"injury", "coach_note", "goal"}

    def test_report_injects_notes_per_view(self, mock_db):
        from app.memory.notes import add_note
        add_note(mock_db, ATHLETE, "coach_note", "私密教练观察", "coach", ATHLETE)
        rep_a = generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="athlete", force=True)
        rep_c = generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach", force=True)
        texts_a = " ".join(s["content"][0] for s in rep_a["sections"])
        texts_c = " ".join(s["content"][0] for s in rep_c["sections"])
        assert "私密教练观察" not in texts_a
        assert "私密教练观察" in texts_c


class TestAntiHardcoding:
    def test_report_text_derived_from_data(self, engine_env):
        """反硬编码：同窗口不同数据 → 文案数字不同且等于实测值。"""
        cfg = get_config()
        _, db_path = engine_env
        db = Database(db_path)
        # 两名运动员同一周，成绩差异明显
        for aid, scores, name in [
            ("111", [9.0] * 40 + [8.5] * 20, "甲"),   # avg 8.83
            ("222", [10.4] * 60, "乙"),                # avg 10.4
        ]:
            db.upsert_profile({"athlete_id": aid, "name": name})
            seed_session(db, aid, f"SA-{aid}", "2026-08-03T01:00:00.000Z", scores[:30])
            seed_session(db, aid, f"SB-{aid}", "2026-08-05T01:00:00.000Z", scores[30:])
            seed_anchor(db, aid, f"SA-{aid}")
        r1 = generate_report(db, "111", "weekly", "2026-W32", view="coach", force=True)
        r2 = generate_report(db, "222", "weekly", "2026-W32", view="coach", force=True)
        t1 = " ".join(s["content"][0] for s in r1["sections"])
        t2 = " ".join(s["content"][0] for s in r2["sections"])
        assert "8.83" in t1 and "10.4" in t2  # 数字来自数据而非硬编码
        assert t1 != t2
        db.close()
