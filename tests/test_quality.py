# -*- coding: utf-8 -*-
"""P0 数据质量护栏单测：不可能分布 / 过小样本的纯函数判定（app/reports/quality.py）。"""
from app.config import DataQuality
from app.reports.quality import assess_quality

DQ = DataQuality(min_shots_for_description=6, score_max=10.9)


def _m(**over) -> dict:
    """构造一份「干净」指标字典，按用例覆盖特定键。"""
    base = {"n_shots": 30, "avg_score": 9.0, "miss_rate": 0.0,
            "far_miss_rate": 0.0, "inner10_rate": 0.0, "hit_rate": 100.0}
    base.update(over)
    return base


class TestAssessQuality:
    def test_clean_data_not_suspect(self):
        """分布自洽、样本充足：无存疑、不抑制判定。"""
        r = assess_quality(_m(), DQ)
        assert r == {"suspect": False, "block_judgement": False, "reasons": []}

    def test_small_sample_flagged_but_not_blocking(self):
        """过小样本：标注存疑但不抑制方向判定（仅统计量不可靠）。"""
        r = assess_quality(_m(n_shots=5), DQ)
        assert r["suspect"] is True and r["block_judgement"] is False
        assert "过小" in r["reasons"][0]

    def test_empty_window_makes_no_quality_claim(self):
        """空窗口：比例与样本下限无意义，交由 data_integrity 标注「空窗口」，此处不报存疑。"""
        r = assess_quality(_m(n_shots=0, hit_rate=0.0, avg_score=None), DQ)
        assert r == {"suspect": False, "block_judgement": False, "reasons": []}

    def test_rate_sum_contradiction_blocks(self):
        """脱靶率过高却报出高均环：超出脱靶率隐含上限 → 结构性矛盾，抑制判定。"""
        r = assess_quality(_m(miss_rate=61.5, far_miss_rate=100.0, avg_score=9.0), DQ)
        assert r["block_judgement"] is True
        assert any("上限" in x for x in r["reasons"])

    def test_far_below_miss_contradiction_blocks(self):
        """远弹率低于脱靶率（0 环箭必为远弹）：矛盾 → 抑制判定。"""
        r = assess_quality(_m(miss_rate=10.0, hit_rate=90.0, far_miss_rate=5.0), DQ)
        assert r["block_judgement"] is True
        assert any("低于脱靶率" in x for x in r["reasons"])

    def test_inner_and_far_over_100_contradiction_blocks(self):
        """内十率与远弹率之和超 100%（两者互斥）：矛盾 → 抑制判定。"""
        r = assess_quality(_m(inner10_rate=50.0, far_miss_rate=60.0), DQ)
        assert r["block_judgement"] is True
        assert any("互斥" in x for x in r["reasons"])

    def test_avg_score_out_of_range_contradiction_blocks(self):
        """含脱靶均环越界：矛盾 → 抑制判定。"""
        r = assess_quality(_m(avg_score=11.5), DQ)
        assert r["block_judgement"] is True
        assert any("越界" in x for x in r["reasons"])

    def test_hit_and_miss_not_assumed_complementary(self):
        """命中率与脱靶率非互补（hit 与 score 独立信号）：不因二者之和≠100% 误报。"""
        r = assess_quality(_m(hit_rate=99.2, miss_rate=0.0, far_miss_rate=2.5), DQ)
        assert r["block_judgement"] is False

    def test_rate_tolerance_absorbs_rounding(self):
        """比例容差 0.2：指标层 1 位小数舍入（远弹 9.9 vs 脱靶 10.0）不误报。"""
        r = assess_quality(_m(miss_rate=10.0, far_miss_rate=9.9), DQ)
        assert r["block_judgement"] is False