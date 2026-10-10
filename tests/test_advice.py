# -*- coding: utf-8 -*-
"""建议层单测：确定性训练建议生成（app/reports/advice.py）。"""
from types import SimpleNamespace

from app.config import Advice
from app.reports.advice import build_advice

CFG = SimpleNamespace(
    advice=Advice(miss_rate_high=10.0, far_miss_high=30.0, inner10_low=20.0,
                  wind_band_delta=0.5, min_band_shots=10),
    wind_bands=[[0, 1.5], [1.5, 2.0], [2.0, 2.5], [2.5, 99]])

CLEAN_DQ = {"suspect": False, "block_judgement": False, "reasons": []}


def _m(**over) -> dict:
    base = {"n_shots": 30, "avg_score": 9.0, "effective_avg_score": 9.0,
            "miss_rate": 0.0, "far_miss_rate": 0.0, "inner10_rate": 50.0, "hit_rate": 100.0}
    base.update(over)
    return base


def _build(m, **over) -> dict:
    kw = dict(conclusion_keys=[], wind_band_avg={}, wind_band_counts={},
              dq=CLEAN_DQ, gate_ok=True, cfg=CFG)
    kw.update(over)
    return build_advice(m, **kw)


class TestAdviceGuards:
    def test_empty_window_has_no_suggestions(self):
        """空窗口：不出建议（真实空状态，不编造）。"""
        assert _build(_m(n_shots=0)) == {"suggestions": [], "evidence": []}

    def test_quality_suspect_refuses_advice(self):
        """数据存疑（结构矛盾）：显式拒绝出建议并留痕。"""
        r = _build(_m(), dq={"suspect": True, "block_judgement": True, "reasons": ["x"]})
        assert r["suggestions"] == ["本窗口数据存疑（分布存在结构性矛盾），暂不给出训练建议"]
        assert r["evidence"][0]["ref"] == "advice_blocked"

    def test_sample_insufficient_adds_caveat(self):
        """样本不足：首条为「仅供参考」声明，不静默出建议。"""
        r = _build(_m(miss_rate=20.0, avg_score=7.0, effective_avg_score=8.75), gate_ok=False)
        assert r["suggestions"][0] == "样本不足，以下建议仅供参考"


class TestStructureAdvice:
    def test_miss_advice_attributes_loss_to_misses_only(self):
        """脱靶率超阈值：均环差归因于脱靶（不说「成绩损失主要来自脱靶」），带均环差证据。"""
        r = _build(_m(miss_rate=20.0, avg_score=7.2, effective_avg_score=9.0))
        text = " ".join(r["suggestions"])
        assert "脱靶" in text
        assert "均环损失" in text
        assert "主要来自脱靶" not in text
        ev = [e for e in r["evidence"] if e["ref"] == "advice_miss"][0]
        assert ev["value"]["gap"] == 1.8

    def test_far_non_miss_dominant(self):
        """非脱靶远弹占比超阈值：提示着点离散偏大。"""
        r = _build(_m(miss_rate=0.0, far_miss_rate=40.0))
        assert any("远弹" in s for s in r["suggestions"])
        assert any(e["ref"] == "advice_far" for e in r["evidence"])

    def test_inner10_low(self):
        """内十率低于下限：提示着点不够靠中心。"""
        r = _build(_m(inner10_rate=10.0))
        assert any("内十率" in s for s in r["suggestions"])
        assert any(e["ref"] == "advice_inner" for e in r["evidence"])

    def test_miss_and_far_co_occur(self):
        """脱靶与非脱靶远弹均超阈值：两条建议并列出现，不互相抑制。"""
        r = _build(_m(miss_rate=15.0, far_miss_rate=60.0, avg_score=6.0, effective_avg_score=8.5))
        refs = {e["ref"] for e in r["evidence"]}
        assert "advice_miss" in refs and "advice_far" in refs

    def test_inner_suppressed_when_structure_already_flagged(self):
        """脱靶/远弹已触发时内十率低不再叠加（内十率低与远弹偏多同源，避免重复提示）。"""
        r = _build(_m(miss_rate=15.0, far_miss_rate=60.0, inner10_rate=5.0,
                      avg_score=6.0, effective_avg_score=8.5))
        assert not any(e["ref"] == "advice_inner" for e in r["evidence"])

    def test_clean_structure_yields_no_structure_advice(self):
        """结构自洽且采样完整：无任何建议（真实空状态）。"""
        assert _build(_m())["suggestions"] == []


class TestWindAdvice:
    def test_wind_band_delta_triggers(self):
        """两档达标样本且档间均环差超阈值：提示风况适应，且用可读档标签。"""
        r = _build(_m(), wind_band_avg={0: 9.5, 1: 8.0}, wind_band_counts={0: 20, 1: 20})
        text = " ".join(r["suggestions"])
        assert "风档" in text
        # 档标签由 band_label 归一（1 位小数），展示层与建议层一致
        assert "[0.0,1.5)" in text and "[1.5,2.0)" in text
        # 高风档更低 → 风况适应问题，给出风感训练建议
        assert "增加风感与瞄准补偿训练" in text
        assert any(e["ref"] == "advice_wind" for e in r["evidence"])

    def test_wind_band_calm_worse_not_advised_as_wind_training(self):
        """低风档反而更低（与风况影响方向相反）：不套用风感训练建议，避免自相矛盾文案。"""
        r = _build(_m(), wind_band_avg={0: 8.0, 1: 9.5}, wind_band_counts={0: 20, 1: 20})
        text = " ".join(r["suggestions"])
        assert "风档" in text and "方向相反" in text
        assert "增加风感与瞄准补偿训练" not in text

    def test_wind_band_below_min_shots_ignored(self):
        """档样本不足：不参与比较，不出风况建议。"""
        r = _build(_m(), wind_band_avg={0: 9.5, 1: 8.0}, wind_band_counts={0: 3, 1: 3})
        assert not any(e["ref"] == "advice_wind" for e in r["evidence"])


class TestTrendAdvice:
    def test_progress_assertive(self):
        r = _build(_m(), conclusion_keys=["progress"])
        assert any("判定进步" in s for s in r["suggestions"])

    def test_regression_reference_is_soft(self):
        """参考档（未达强断言门槛）：只给方向提示，不越界下结论。"""
        r = _build(_m(), conclusion_keys=["regression_ref"])
        assert any("方向提示" in s for s in r["suggestions"])
        assert not any("判定退步" in s for s in r["suggestions"])


class TestDataGapAdvice:
    def test_missing_sampling_not_duplicated_in_advice(self):
        """缺风速/心率不再在建议层重复提示（已由 data_integrity 段显式标注）。"""
        r = _build(_m())
        assert not any("无风速数据" in s for s in r["suggestions"])
        assert not any("无心率数据" in s for s in r["suggestions"])