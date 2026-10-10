# -*- coding: utf-8 -*-
"""M4：规则引擎测试（MDC 方向/阈值/降级、禁语、样本门槛）。"""
import pytest

from app.config import get_config
from app.reports import rules as R


class TestJudgeMetric:
    def test_avg_score_progress(self, engine_env):
        cfg = get_config()
        # avgScore direction=+1：now>ref → progress
        res = R.judge_metric(cfg, "avgScore", 9.8, 9.4)
        assert res["judge"] == "progress"
        assert res["delta"] == pytest.approx(0.4)
        assert res["degraded"] is False

    def test_avg_score_regression(self, engine_env):
        res = R.judge_metric(get_config(), "avgScore", 9.0, 9.4)
        assert res["judge"] == "regression"

    def test_avg_score_below_te_is_undecidable(self, engine_env):
        # now 9.6 落入最高档（≤10.6）→ TE 0.12：差 0.1 ≤ TE → 低于噪声底，不出判定
        res = R.judge_metric(get_config(), "avgScore", 9.6, 9.5)
        assert res["judge"] is None
        assert res["tier"] == "below_noise"
        assert res["degraded"] is False

    def test_mcr_direction_minus1(self, engine_env):
        # mcrT direction=-1：now<ref（用时更短）→ progress
        res = R.judge_metric(get_config(), "mcrT", 0.40, 0.50)
        assert res["judge"] == "progress"
        res2 = R.judge_metric(get_config(), "mcrT", 0.60, 0.50)
        assert res2["judge"] == "regression"

    def test_missing_now_or_ref_no_conclusion(self, engine_env):
        res = R.judge_metric(get_config(), "avgScore", None, 9.4)
        assert res["judge"] is None and res["degraded"] is True

    def test_degraded_when_mdc_source_empty(self, degraded_env):
        cfg = get_config()
        assert cfg.mdc_source is None
        res = R.judge_metric(cfg, "avgScore", 9.8, 9.4)
        assert res["judge"] is None  # 只描述不判定
        assert res["delta"] == pytest.approx(0.4)
        assert res["degraded"] is True

    def test_hr_volatility_threshold_pending_consensus(self, engine_env):
        # config 中 hrVolatility.threshold=null（M4.5 前）→ 降级
        res = R.judge_metric(get_config(), "hrVolatility", 12.0, 10.0)
        assert res["judge"] is None
        assert res["degraded"] is True
        assert res["mdc"] is None


class TestChangeCaliberTier:
    """B13：Hopkins TE/SWC 两级三档 + 天花板分区间（按当前值取档）。"""

    def test_noise_band_is_steady(self, engine_env):
        # now 9.64 落最高档（TE 0.12 / SWC 0.15）：差 0.14 落在 (TE, SWC] → 正常波动
        res = R.judge_metric(get_config(), "avgScore", 9.64, 9.5)
        assert res["judge"] == "steady"
        assert res["tier"] == "noise_band"
        assert res["te"] == 0.12 and res["swc"] == 0.15

    def test_reference_tier_direction_hint(self, engine_env):
        # 差 0.2 超 SWC 0.15 但未达 MDC95 0.333 → 方向提示（参考，未达强断言门槛）
        res = R.judge_metric(get_config(), "avgScore", 9.7, 9.5)
        assert res["judge"] == "progress"
        assert res["tier"] == "reference"
        assert res["mdc"] == pytest.approx(0.333)

    def test_assertive_tier_full_judgement(self, engine_env):
        # 差 0.4 ≥ MDC95 0.333 → 强断言方向判定
        res = R.judge_metric(get_config(), "avgScore", 9.9, 9.5)
        assert res["judge"] == "progress"
        assert res["tier"] == "assertive"

    def test_mdc95_derived_from_te(self, engine_env):
        res = R.judge_metric(get_config(), "avgScore", 9.9, 9.5)
        assert res["mdc"] == pytest.approx(R.MDC95_FACTOR * 0.12, abs=1e-3)

    def test_ceiling_band_shrinks_thresholds(self, engine_env):
        # 同一差值 0.3：低分档（now 7.5，TE 0.45）不可判定；高分档（now 9.8，TE 0.12）达参考档
        low = R.judge_metric(get_config(), "avgScore", 7.5, 7.2)
        high = R.judge_metric(get_config(), "avgScore", 9.8, 9.5)
        assert low["tier"] == "below_noise"
        assert high["tier"] == "reference"

    def test_legacy_single_threshold_fallback(self, engine_env):
        # mcrT 未配置 TE/SWC → 回退旧单阈值：差 0.10 ≥ 0.05 → 强断言；差 0.02 < 0.05 → 正常波动
        res = R.judge_metric(get_config(), "mcrT", 0.40, 0.50)
        assert res["judge"] == "progress" and res["tier"] == "assertive"
        res2 = R.judge_metric(get_config(), "mcrT", 0.52, 0.50)
        assert res2["judge"] == "steady" and res2["tier"] == "noise_band"

    def test_bands_must_be_strictly_ascending(self):
        from pydantic import ValidationError

        from app.config import ChangeBand, MDCSpec
        with pytest.raises(ValidationError):
            MDCSpec(direction=1, bands=[ChangeBand(max=9.5), ChangeBand(max=8.0)])


class TestRenderRule:
    def test_render_progress(self, engine_env):
        cfg = get_config()
        rule = R.RULES["avg_vs_anchor"]
        res = R.render_rule(cfg, rule, {"judge": "progress", "degraded": False, "delta": 0.4},
                            {"now": 9.8, "ref": 9.4, "delta": 0.4, "ref_date": "2026-07-20", "mdc": 0.3})
        assert "进步" in res["conclusion"]
        assert "9.8" in res["conclusion"]

    def test_render_below_noise(self, engine_env):
        res = R.render_rule(get_config(), R.RULES["avg_vs_anchor"],
                            {"judge": None, "tier": "below_noise", "degraded": False, "delta": 0.1},
                            {"now": 9.6, "ref": 9.5, "delta": 0.1, "delta_signed": 0.1,
                             "ref_date": "2026-07-20", "mdc": 0.333, "mdc95": 0.333,
                             "te": 0.12, "swc": 0.15})
        assert "低于噪声底" in res["conclusion"]

    def test_render_reference(self, engine_env):
        res = R.render_rule(get_config(), R.RULES["avg_vs_anchor"],
                            {"judge": "progress", "tier": "reference", "degraded": False, "delta": 0.2},
                            {"now": 9.7, "ref": 9.5, "delta": 0.2, "delta_signed": "+0.20",
                             "ref_date": "2026-07-20", "mdc": 0.333, "mdc95": 0.333,
                             "te": 0.12, "swc": 0.15})
        assert "方向提示" in res["conclusion"] and "+0.20" in res["conclusion"]

    def test_render_degrade_uses_degrade_template(self, degraded_env):
        cfg = get_config()
        res = R.render_rule(cfg, R.RULES["avg_vs_anchor"],
                            {"judge": None, "degraded": True, "delta": 0.4},
                            {"now": 9.8, "ref": 9.4, "delta": 0.4, "ref_date": "2026-07-20", "mdc": "-"})
        assert "未判定" in res["conclusion"]

    def test_forbidden_phrase_raises(self, engine_env, monkeypatch):
        cfg = get_config()
        monkeypatch.setattr(cfg, "forbidden_phrases", ["练它即提分"])
        res = {"judge": "progress", "degraded": False, "delta": 0.4}
        vars_ = {"now": 9.8, "ref": 9.4, "delta": 0.4, "ref_date": "2026-07-20", "mdc": 0.3}
        # 手动构造命中禁语的模板（单测防回归：禁语必须拦截）
        rule = R.RuleSpec(id="x", metric="avgScore",
                          template_progress="练它即提分 {now} 环", template_regression="r",
                          template_reference="ref", template_steady="s",
                          template_below_noise="bn", template_degrade="d")
        with pytest.raises(ValueError):
            R.render_rule(cfg, rule, res, vars_)

    def test_validate_rules_missing_metric_key(self, engine_env, monkeypatch):
        cfg = get_config()
        monkeypatch.setattr(cfg, "mdc", {k: v for k, v in cfg.mdc.items() if k != "avgScore"})
        with pytest.raises(ValueError):
            R.validate_rules(cfg)


class TestSampleGate:
    def test_daily_threshold(self, engine_env):
        cfg = get_config()
        assert R.sample_gate(cfg, "daily", 20, 1) is True
        assert R.sample_gate(cfg, "daily", 19, 1) is False

    def test_weekly_requires_two_sessions(self, engine_env):
        cfg = get_config()
        assert R.sample_gate(cfg, "weekly", 60, 1) is False  # 箭够但训练次数不足
        assert R.sample_gate(cfg, "weekly", 60, 2) is True

    def test_monthly_quarterly_yearly(self, engine_env):
        cfg = get_config()
        assert R.sample_gate(cfg, "monthly", 87, 1) is True
        assert R.sample_gate(cfg, "quarterly", 260, 1) is True
        assert R.sample_gate(cfg, "yearly", 1040, 1) is True
        assert R.sample_gate(cfg, "yearly", 1039, 1) is False
