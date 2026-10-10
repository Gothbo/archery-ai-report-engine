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
        level = [s for s in rep["sections"] if s["key"] == "level"][0]
        text = " ".join(level["content"])
        assert "降级期" in text and "暂无进步/退步判定" in text  # 降级提示（ADR-0002）
        assert "未判定" in text  # 锚点降级模板：只描述不判定
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


class TestDualCaliberScore:
    """T2 验收：window_score 并列双口径 + 脱靶率；口径版本递增使旧缓存失效（ADR-0001）。"""

    def test_dual_average_score_display(self, engine_env):
        """已知脱靶样本：含脱靶均环 / 有效箭均环 / 脱靶率并列且数值正确，内十/远弹/命中保留。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        # 10 箭脱靶（0 环）+ 20 箭 8 环：含脱靶 160/30=5.33，有效箭 8.00，脱靶率 10/30=33.3%
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [0.0] * 10 + [8.0] * 20,
                     wind=[0.8] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        ws = [s for s in draft.report["sections"] if s["key"] == "window_score"][0]
        text = ws["content"][0]
        assert "含脱靶均环 5.33" in text
        assert "有效箭均环 8.00" in text
        assert "脱靶率 33.3%" in text
        assert "内十率" in text and "远弹率" in text and "命中率" in text
        db.close()

    def test_cache_invalidation_on_caliber_version_bump(self, engine_env, tmp_path):
        """递增报告口径版本 → 旧缓存不再命中，重新生成返回新 report_id。"""
        import json

        import app.config as cfgmod

        cfg_dict, db_path = engine_env
        cfg_path = tmp_path / "config.json"
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, wind=[0.8] * 30)

        r1 = generate_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        assert r1["cached"] is False
        r2 = generate_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        assert r2["cached"] is True and r2["report_id"] == r1["report_id"]

        cfg_dict["report_caliber_version"] = "v-next"
        cfg_path.write_text(json.dumps(cfg_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        cfgmod.get_config.cache_clear()

        r3 = generate_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        assert r3["cached"] is False
        assert r3["report_id"] != r1["report_id"]
        db.close()


class TestDegradedLevelSection:
    """T3 验收：降级期 level 段出降级提示 + 滚动基线水平描述；有 MDC 仍出方向判定（ADR-0002）。"""

    # 方向判定词：降级期除提示行外不得出现
    DIRECTION_WORDS = ("进步", "退步", "提升", "下降", "收窄", "增大", "回落", "升高", "缩短", "延长")

    @staticmethod
    def _level(report: dict) -> dict:
        return [s for s in report["sections"] if s["key"] == "level"][0]

    def test_degraded_level_shows_hint_without_direction_judgement(self, degraded_env):
        """降级期：level 段出现「降级期，暂无进步/退步判定」提示，其余行不含任何方向判定词。"""
        cfg, db_path = degraded_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.5] * 30, wind=[1.0] * 30)
        seed_anchor(db, ATHLETE, "S-A")
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        lines = self._level(draft.report)["content"]
        hint = [ln for ln in lines if "降级期" in ln]
        assert hint, "降级期 level 段应出现降级提示"
        assert "暂无进步/退步判定" in hint[0]
        for line in [ln for ln in lines if "降级期" not in ln]:
            assert not any(w in line for w in self.DIRECTION_WORDS), f"降级期不应出方向判定词：{line}"
        db.close()

    def test_degraded_level_describes_rolling_baseline(self, degraded_env):
        """降级期：level 段改述滚动基线水平（只描述不判定）；基线只取窗口前的箭。"""
        cfg, db_path = degraded_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        seed_session(db, ATHLETE, "S-B", "2026-08-05T01:00:00.000Z", [9.5] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-B", session_id="S-B", view="coach")
        text = " ".join(self._level(draft.report)["content"])
        assert "滚动基线" in text
        assert "窗口前近 30 箭" in text   # 基线 = 窗口前的 S-A，不含窗口内 S-B
        assert "9.00" in text            # S-A 均环
        assert "+0.50" in text           # 本窗口 9.50 较基线 +0.50
        db.close()

    def test_level_falls_back_to_rolling_baseline_without_anchor(self, engine_env):
        """有 MDC 口径但无锚点：level 段改述窗口前滚动基线水平，不再是无信息占位（只描述不判定）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        seed_session(db, ATHLETE, "S-B", "2026-08-05T01:00:00.000Z", [9.5] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-B", session_id="S-B", view="coach")
        text = " ".join(self._level(draft.report)["content"])
        assert "滚动基线" in text and "9.00" in text and "+0.50" in text
        assert not any(w in text for w in self.DIRECTION_WORDS), text  # 无锚点 → 只描述不判定
        db.close()

    def test_rolling_baseline_excludes_shots_after_window(self, engine_env):
        """滚动基线只取窗口前的箭：窗口之后的箭不得进入基线（时间语义）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [8.0] * 30)
        seed_session(db, ATHLETE, "S-B", "2026-08-05T01:00:00.000Z", [9.5] * 30)
        seed_session(db, ATHLETE, "S-C", "2026-09-01T01:00:00.000Z", [10.0] * 30)  # 窗口之后
        draft = build_report(db, ATHLETE, "daily", "daily:S-B", session_id="S-B", view="coach")
        text = " ".join(self._level(draft.report)["content"])
        assert "8.00" in text        # 基线 = 窗口前 S-A
        assert "10.00" not in text   # 窗口后的 S-C 不进基线
        db.close()

    def test_mdc_configured_still_judges_direction(self, engine_env):
        """有 MDC 配置：level 段仍出方向判定（较锚点/与锚点），且不出现降级提示（回归不破）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.5] * 30, wind=[1.0] * 30)
        seed_anchor(db, ATHLETE, "S-A")
        seed_session(db, ATHLETE, "S-B", "2026-08-05T01:00:00.000Z", [9.8] * 30, wind=[1.0] * 30)
        draft = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach")
        lines = self._level(draft.report)["content"]
        assert any(("较锚点" in ln) or ("与锚点" in ln) for ln in lines)
        assert not any("降级期" in ln for ln in lines)
        db.close()


class TestPrimaryBowAggregation:
    """T4 验收：窗口按主弓种（箭数最多）聚合，其余弓种显式标注不计入，报告体带 bow_type（ADR-0004）。"""

    @staticmethod
    def _section(report: dict, key: str) -> dict:
        return [s for s in report["sections"] if s["key"] == key][0]

    def test_single_bow_window_counts_all_shots(self, engine_env):
        """单弓种窗口：全部箭计入指标，bow_type 为该弓种，无「未计入」标注。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, bow_type="反曲弓")
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        rep = draft.report
        assert rep["bow_type"] == "反曲弓"
        assert "（n=30）" in self._section(rep, "window_score")["content"][0]
        assert "未计入" not in " ".join(self._section(rep, "data_integrity")["content"])
        db.close()

    def test_multi_bow_window_filters_to_primary_and_annotates_others(self, engine_env):
        """多弓种窗口：仅主弓种参与指标，其余弓种在 data_integrity 标注箭数与名称。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 20, bow_type="反曲弓")
        seed_session(db, ATHLETE, "S-B", "2026-08-04T01:00:00.000Z", [5.0] * 10, bow_type="复合弓")
        draft = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach")
        rep = draft.report
        assert rep["bow_type"] == "反曲弓"
        score = self._section(rep, "window_score")["content"][0]
        assert "（n=20）" in score  # 只计入主弓种 20 箭
        assert "9.00" in score     # 均环取自主弓种，未被 5.0 的复合弓拉低
        integrity = " ".join(self._section(rep, "data_integrity")["content"])
        assert "另有 10 箭为 复合弓 弓种，未计入" in integrity
        db.close()

    def test_primary_bow_tie_break_is_deterministic(self, engine_env):
        """箭数并列时按弓种名升序取主弓种，保证跨次生成结果一致。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 10, bow_type="复合弓")
        seed_session(db, ATHLETE, "S-B", "2026-08-04T01:00:00.000Z", [9.5] * 10, bow_type="反曲弓")
        draft = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach")
        assert draft.report["bow_type"] == "反曲弓"  # 并列时 '反' < '复'（码点序）
        db.close()

    def test_level_ignores_anchor_of_other_bow(self, engine_env):
        """主弓种窗口不拿另一弓种的锚点做对比（ADR-0004：不同项目基线不混用）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        # 窗口：反曲弓两场（过周报 ≥2 场门槛）；唯一锚点却属于复合弓
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 20, bow_type="反曲弓")
        seed_session(db, ATHLETE, "S-C", "2026-08-05T01:00:00.000Z", [9.0] * 20, bow_type="反曲弓")
        seed_session(db, ATHLETE, "S-B", "2026-07-01T01:00:00.000Z", [8.0] * 30, bow_type="复合弓")
        seed_anchor(db, ATHLETE, "S-B", bow_type="复合弓")
        draft = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach")
        text = " ".join(self._section(draft.report, "level")["content"])
        assert "较锚点" not in text and "与锚点" not in text  # 不跨弓种对比
        assert "无锚点" in text  # 本弓种无锚点 → 显式说明
        assert draft.report["bow_type"] == "反曲弓"
        db.close()

    def test_sample_gate_counts_only_primary_bow_sessions(self, engine_env):
        """样本门槛的训练次数按主弓种过滤：非主弓种场次不计入周报 ≥2 场护栏。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        # 反曲弓 1 场 30 箭（主弓种）；复合弓 1 场 30 箭 → 主弓种仅 1 场，未达周报 ≥2 场
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, bow_type="反曲弓")
        seed_session(db, ATHLETE, "S-B", "2026-08-04T01:00:00.000Z", [5.0] * 30, bow_type="复合弓")
        draft = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach")
        assert "样本不足" in self._section(draft.report, "sample_gate")["content"][0]
        db.close()


class TestWindowRangeMetadata:
    """T5 验收：报告体含 window_start / window_end，取值与窗口边界一致（daily 用场次时间）。"""

    def test_daily_uses_session_time(self, engine_env):
        """daily：本期区间取场次时间（点区间，start == end）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        rep = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach").report
        assert rep["window_start"] == "2026-08-03T01:00:00.000Z"
        assert rep["window_end"] == "2026-08-03T01:00:00.000Z"
        db.close()

    def test_weekly_matches_window_bounds(self, engine_env):
        """weekly：本期区间 = 窗口 [start, end) 边界（本地时区切窗 → UTC 存储，左闭右开）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        rep = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach").report
        assert rep["window_start"] == "2026-08-02T16:00:00.000Z"  # 2026-08-03 00:00 (Asia/Shanghai)
        assert rep["window_end"] == "2026-08-09T16:00:00.000Z"    # 2026-08-10 00:00（右开）
        db.close()

    def test_monthly_quarterly_yearly_match_window_bounds(self, engine_env):
        """monthly/quarterly/yearly：本期区间同样等于各自窗口 [start, end) 边界。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        expected = {
            ("monthly", "2026-08"): ("2026-07-31T16:00:00.000Z", "2026-08-31T16:00:00.000Z"),
            ("quarterly", "2026Q3"): ("2026-06-30T16:00:00.000Z", "2026-09-30T16:00:00.000Z"),
            ("yearly", "2026"): ("2025-12-31T16:00:00.000Z", "2026-12-31T16:00:00.000Z"),
        }
        for (gran, key), (start, end) in expected.items():
            rep = build_report(db, ATHLETE, gran, key, view="coach").report
            assert (rep["window_start"], rep["window_end"]) == (start, end), gran
        db.close()


class TestViewCoachExtraGating:
    """T6 验收：正文两视图内容一致（备注为既定例外）；运维字段仅 coach 返回（ADR-0005）。"""

    def _section(self, report: dict, key: str) -> dict:
        return [s for s in report["sections"] if s["key"] == key][0]

    def test_sections_identical_across_views(self, engine_env):
        """双方可见备注（injury/goal）场景：coach / athlete 的 sections 逐字一致。"""
        from app.memory.notes import add_note
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        add_note(db, ATHLETE, "injury", "右肩轻微酸痛", "athlete", ATHLETE)
        rep_a = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="athlete").report
        rep_c = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach").report
        assert rep_a["sections"] == rep_c["sections"]
        db.close()

    def test_ops_fields_gated_to_coach(self, engine_env):
        """运维字段（anchor_rebuild_hint / evidence 明细）只在 coach 的 coach_extra；
        正文 sections 不含 evidence 明细，顶层也不再挂 anchor_rebuild_hint。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        rep_c = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach").report
        rep_a = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="athlete").report
        # coach：附注区含运维字段
        assert "anchor_rebuild_hint" in rep_c["coach_extra"]
        assert rep_c["coach_extra"]["evidence"]
        # athlete：附注区保留非运维键，但不含运维字段
        assert set(rep_a["coach_extra"]) <= {"warnings", "load", "rolling_baseline"}
        assert "anchor_rebuild_hint" not in rep_a["coach_extra"]
        assert "evidence" not in rep_a["coach_extra"]
        # 正文两视图都不再携带 evidence 明细；顶层 anchor_rebuild_hint 已移除
        assert all("evidence" not in s for s in rep_c["sections"])
        assert "anchor_rebuild_hint" not in rep_c
        db.close()

    def test_athlete_notes_only_injury_goal(self, engine_env):
        """备注过滤沿用 notes_visibility：athlete 只见 injury/goal，coach 全量。"""
        from app.memory.notes import add_note
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30)
        add_note(db, ATHLETE, "coach_note", "私密教练观察", "coach", ATHLETE)
        rep_a = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="athlete").report
        rep_c = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach").report
        aux_a = " ".join(self._section(rep_a, "auxiliary")["content"])
        aux_c = " ".join(self._section(rep_c, "auxiliary")["content"])
        assert "私密教练观察" not in aux_a
        assert "私密教练观察" in aux_c
        db.close()

    def test_only_coach_extra_and_notes_differ(self, engine_env):
        """两视图差异只发生在 coach_extra 与 auxiliary 备注：其余五段逐字一致。"""
        from app.memory.notes import add_note
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, wind=[0.8] * 30)
        add_note(db, ATHLETE, "coach_note", "私密教练观察", "coach", ATHLETE)
        rep_a = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="athlete").report
        rep_c = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach").report
        assert _keys(rep_a) == _keys(rep_c) == CANONICAL_SIX
        for key in CANONICAL_SIX:
            if key == "auxiliary":  # 备注为既定例外
                continue
            assert self._section(rep_a, key) == self._section(rep_c, key), key
        assert rep_a["coach_extra"] != rep_c["coach_extra"]
        db.close()


class TestDataQualityGuardrail:
    """P0 验收：不可能分布 / 过小样本在 data_integrity 显式标注「数据存疑」；
    结构矛盾时抑制 level 段方向判定，避免用不可信数据误导教练。"""

    @staticmethod
    def _section(report: dict, key: str) -> dict:
        return [s for s in report["sections"] if s["key"] == key][0]

    def test_small_sample_flagged_in_data_integrity(self, engine_env):
        """过小样本（n=5 < 6）：data_integrity 显式标注「数据存疑」。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 5)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        integrity = " ".join(self._section(draft.report, "data_integrity")["content"])
        assert "数据存疑" in integrity and "过小" in integrity
        db.close()

    def test_clean_sample_not_flagged(self, engine_env):
        """分布自洽、样本充足：data_integrity 不出现「数据存疑」。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, wind=[0.8] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        integrity = " ".join(self._section(draft.report, "data_integrity")["content"])
        assert "数据存疑" not in integrity
        db.close()

    def test_contradiction_suppresses_direction_judgement(self, engine_env, monkeypatch):
        """结构矛盾（护栏判 block）：level 段不出方向判定并显式说明，data_integrity 展示原因。"""
        import app.reports.generator as gen
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.5] * 30, wind=[1.0] * 30)
        seed_anchor(db, ATHLETE, "S-A")
        seed_session(db, ATHLETE, "S-B", "2026-08-05T01:00:00.000Z", [9.8] * 30, wind=[1.0] * 30)
        monkeypatch.setattr(gen.Q, "assess_quality", lambda m, dq: {
            "suspect": True, "block_judgement": True,
            "reasons": ["远弹率 0.0% 低于脱靶率 61.5%（0 环箭必为远弹）"]})
        draft = build_report(db, ATHLETE, "weekly", "2026-W32", view="coach")
        level = " ".join(self._section(draft.report, "level")["content"])
        integrity = " ".join(self._section(draft.report, "data_integrity")["content"])
        assert "数据存疑" in level and "不出方向判定" in level
        assert "数据存疑" in integrity and "低于脱靶率" in integrity
        # 结构矛盾时不落任何强断言方向结论
        assert not any(c["conclusion_key"] in ("progress", "regression") for c in draft.conclusions)
        db.close()


class TestAdviceLayer:
    """P0 验收：建议层填充 report.suggestions（由判断信号确定性生成）；
    两视图正文一致，evidence 仅教练附注区可见；空窗口/数据存疑时给显式空状态或拒绝。"""

    def test_miss_heavy_window_populates_advice(self, engine_env):
        """脱靶率高的窗口：suggestions 出脱靶建议，evidence 仅教练附注区。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z",
                     [0.0] * 15 + [9.0] * 15, wind=[0.8] * 30, hr=[70] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        assert any("脱靶" in x for x in draft.report["suggestions"])
        assert any(e["ref"] == "advice_miss"
                   for e in draft.report["coach_extra"]["advice_evidence"])
        db.close()

    def test_advice_body_identical_across_views(self, engine_env):
        """suggestions 属共享正文：两视图一致；advice_evidence 仅教练可见。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30,
                     wind=[0.8] * 30, hr=[70] * 30)
        a = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="athlete").report
        c = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach").report
        assert a["suggestions"] == c["suggestions"]
        assert "advice_evidence" not in a["coach_extra"]
        assert "advice_evidence" in c["coach_extra"]
        db.close()

    def test_empty_window_has_empty_suggestions(self, engine_env):
        """空窗口：suggestions 为空（真实空状态）。"""
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.0] * 30, wind=[0.8] * 30)
        draft = build_report(db, ATHLETE, "daily", "daily:S-EMPTY",
                             session_id="S-EMPTY", view="coach")
        assert draft.report["suggestions"] == []
        db.close()

    def test_contradiction_blocks_advice(self, engine_env, monkeypatch):
        """数据存疑（结构矛盾）：不出建议，显式拒绝。"""
        import app.reports.generator as gen
        cfg, db_path = engine_env
        db = Database(db_path)
        seed_session(db, ATHLETE, "S-A", "2026-08-03T01:00:00.000Z", [9.5] * 30, wind=[1.0] * 30)
        monkeypatch.setattr(gen.Q, "assess_quality", lambda m, dq: {
            "suspect": True, "block_judgement": True, "reasons": ["矛盾"]})
        draft = build_report(db, ATHLETE, "daily", "daily:S-A", session_id="S-A", view="coach")
        assert draft.report["suggestions"] == ["本窗口数据存疑（分布存在结构性矛盾），暂不给出训练建议"]
        db.close()
