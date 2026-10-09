# -*- coding: utf-8 -*-
"""B1-7：上下文组装器测试（确定性检索：档案/报告/历史结论/备注/滚动基线 + sources + suggested_note）。"""
import pytest

from app.memory.notes import add_note
from app.rag.context import assemble_context, extract_numbers
from app.reports.generator import generate_report
from tests.conftest import ATHLETE, seed_anchor


class TestContextErrors:
    def test_unknown_athlete(self, db):
        with pytest.raises(ValueError) as ei:
            assemble_context(db, "NOPE", "这周怎么样？")
        assert "不存在" in str(ei.value)

    def test_no_report_yet(self, db):
        db.upsert_profile({"athlete_id": ATHLETE, "name": "张明"})
        with pytest.raises(ValueError) as ei:
            assemble_context(db, ATHLETE, "这周怎么样？")
        assert "暂无已生成报告" in str(ei.value)

    def test_bad_granularity(self, mock_db):
        with pytest.raises(ValueError) as ei:
            assemble_context(mock_db, ATHLETE, "q", granularity="decade", window_key="2026")
        assert "granularity" in str(ei.value)

    def test_granularity_without_window(self, mock_db):
        with pytest.raises(ValueError) as ei:
            assemble_context(mock_db, ATHLETE, "q", granularity="weekly")
        assert "window_key" in str(ei.value)


class TestContextAssembly:
    def _prep(self, mock_db, *, note=None, view="coach"):
        """生成一份周报告 + 可选备注，返回 (ctx)。"""
        if note:
            add_note(mock_db, ATHLETE, note[0], note[1], note[2], ATHLETE)
        generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach", force=True)
        return assemble_context(mock_db, ATHLETE, "这周为什么进步了？", view=view)

    def test_skeleton_and_numbers(self, mock_db):
        ctx = self._prep(mock_db)
        assert ctx["granularity"] == "weekly"
        assert ctx["window_key"] == "2026-W32"
        assert ctx["report_id"]
        assert "本周成绩" in ctx["skeleton"]
        assert ctx["numbers"], "骨架必须含数字（G2 依赖）"
        assert ctx["version"]

    def test_sources_include_report_and_baseline(self, mock_db):
        ctx = self._prep(mock_db)
        types = {s["type"] for s in ctx["sources"]}
        assert "report" in types
        assert "baseline" in types  # mock 导入会建滚动基线
        report_src = next(s for s in ctx["sources"] if s["type"] == "report")
        assert report_src["ref"] == "weekly:2026-W32"

    def test_notes_and_history_in_context(self, mock_db):
        ctx = self._prep(mock_db, note=("injury", "右肩轻微酸痛", "athlete"))
        assert any("备注" in line for line in ctx["skeleton"].splitlines())
        # 备注溯源
        assert any(s["type"] == "note" for s in ctx["sources"])

    def test_view_filters_notes(self, mock_db):
        ctx_a = self._prep(mock_db, note=("coach_note", "私密教练观察", "coach"), view="athlete")
        ctx_c = self._prep(mock_db, note=("coach_note", "私密教练观察", "coach"), view="coach")
        assert "私密教练观察" not in ctx_a["skeleton"]
        assert "私密教练观察" in ctx_c["skeleton"]

    def test_history_conclusion_appears(self, mock_db):
        # 建锚点 + 生成两次报告 → 有同粒度 judgement 历史结论
        sid = mock_db.sessions_of_athlete(ATHLETE)[0]["session_id"]
        seed_anchor(mock_db, ATHLETE, sid)
        generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach", force=True)
        generate_report(mock_db, ATHLETE, "weekly", "2026-W33", view="coach", force=True)
        ctx = assemble_context(mock_db, ATHLETE, "上周如何？", view="coach")
        assert any("历史结论" in line for line in ctx["skeleton"].splitlines())
        assert any(s["type"] == "memory" for s in ctx["sources"])

    def test_anchor_enables_judgement_section(self, mock_db):
        sid = mock_db.sessions_of_athlete(ATHLETE)[0]["session_id"]
        seed_anchor(mock_db, ATHLETE, sid)
        generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach", force=True)
        ctx = assemble_context(mock_db, ATHLETE, "有进步吗？", view="coach")
        assert any("锚点" in line for line in ctx["skeleton"].splitlines())


class TestSuggestedNote:
    def test_keyword_hit_returns_card(self, mock_db):
        add_note(mock_db, ATHLETE, "goal", "冲击 9.8 均环", "athlete", ATHLETE)
        generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach", force=True)
        ctx = assemble_context(mock_db, ATHLETE, "怎么调整训练目标？", view="coach")
        assert ctx["suggested_note"] is not None
        assert ctx["suggested_note"]["readonly"] is True  # D4：只展示不落库

    def test_no_keyword_no_card(self, mock_db):
        add_note(mock_db, ATHLETE, "goal", "冲击 9.8 均环", "athlete", ATHLETE)
        generate_report(mock_db, ATHLETE, "weekly", "2026-W32", view="coach", force=True)
        ctx = assemble_context(mock_db, ATHLETE, "这周风大吗？", view="coach")
        assert ctx["suggested_note"] is None


class TestExtractNumbers:
    def test_plain(self):
        assert extract_numbers("均环 10.16（n=120）") == [10.16, 120]

    def test_no_numbers(self):
        assert extract_numbers("没有数字的句子") == []

    def test_leading_ordinal_not_counted(self):
        # 行首列表序号不是数据（G2 误报根因）
        assert extract_numbers("1. 技术更稳定\n2. 心态更专注\n3. 节奏更干脆") == []

    def test_leading_ordinal_keeps_real_numbers(self):
        assert extract_numbers("1. 内十率 75.0%\n2. 远弹率 0.0%") == [75.0, 0.0]

    def test_leading_decimal_kept(self):
        # 行首小数不得被当成序号剔除
        assert extract_numbers("10.16 环是本场平均") == [10.16]

    def test_bracket_ordinal_not_counted(self):
        assert extract_numbers("（1）撒放更干脆\n（2）弹着更集中") == []

    def test_date_format_equivalence(self):
        # 骨架用「2026-06-29」，模型常回写成「2026年6月29日」，两者必须等价
        assert extract_numbers("2026-06-29") == extract_numbers("2026年6月29日")

    def test_date_normalized(self):
        assert extract_numbers("锚点 2026-06-29 采集") == [20260629.0]
