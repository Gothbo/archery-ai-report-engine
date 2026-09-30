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

    def test_avg_score_below_mdc_is_steady(self, engine_env):
        # 阈值 0.30：0.2 差 → steady
        res = R.judge_metric(get_config(), "avgScore", 9.6, 9.4)
        assert res["judge"] == "steady"

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


class TestRenderRule:
    def test_render_progress(self, engine_env):
        cfg = get_config()
        rule = R.RULES["avg_vs_anchor"]
        res = R.render_rule(cfg, rule, {"judge": "progress", "degraded": False, "delta": 0.4},
                            {"now": 9.8, "ref": 9.4, "delta": 0.4, "ref_date": "2026-07-20", "mdc": 0.3})
        assert "进步" in res["conclusion"]
        assert "9.8" in res["conclusion"]

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
                          template_steady="s", template_degrade="d")
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
