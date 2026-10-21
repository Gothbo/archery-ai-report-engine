# -*- coding: utf-8 -*-
"""规则引擎智能指导纯函数单测（app/reports/rule_guidance.py）。"""
from app.reports.rule_guidance import build_rule_guidance


def _rg(**over):
    kw = dict(suggestions=[], advice_evidence=[], metrics={"n_shots": 30, "avg_score": 9.0, "miss_rate": 0.0},
              conclusions={})
    kw.update(over)
    return build_rule_guidance(**kw)


class TestEmptyStates:
    def test_no_shots(self):
        r = _rg(metrics={"n_shots": 0}, suggestions=["不应出现"])
        assert r["empty_reason"] == "no_shots"
        assert r["bullets"] == []
        assert "无箭" in r["summary"]
        assert "不应出现" not in r["summary"]

    def test_data_suspect(self):
        sug = ["本窗口数据存疑（分布存在结构性矛盾），暂不给出训练建议"]
        r = _rg(suggestions=sug, advice_evidence=[{"type": "fact", "ref": "advice_blocked", "value": {}}])
        assert r["empty_reason"] == "data_suspect"
        assert r["bullets"][0]["source"] == "advice_blocked"
        assert "数据存疑" in r["summary"]

    def test_no_signals(self):
        r = _rg(suggestions=[])
        assert r["empty_reason"] == "no_signals"
        assert r["bullets"] == []
        assert "未触发" in r["summary"]


class TestBulletsFromAdvice:
    def test_pairs_evidence_refs(self):
        sug = [
            "样本不足，以下建议仅供参考",
            "脱靶率 20.0%，含脱靶均环 7.20 较有效箭均环 9.00 低 1.80 环（脱靶造成的均环损失）；建议优先稳定撒放与命中",
        ]
        ev = [{"type": "calc", "ref": "advice_miss", "value": {}}]
        r = _rg(suggestions=sug, advice_evidence=ev,
                metrics={"n_shots": 20, "avg_score": 7.2, "miss_rate": 20.0})
        assert r["empty_reason"] is None
        assert r["bullets"][0]["source"] == "caveat_sample"
        assert r["bullets"][1]["source"] == "advice_miss"
        assert "脱靶" in r["bullets"][1]["text"]
        assert "共 2 条" in r["summary"]

    def test_plateau_appended_once(self):
        sug = ["本窗口较锚点判定进步，建议保持当前训练安排并巩固"]
        ev = [{"type": "fact", "ref": "advice_progress", "value": {}}]
        plateau = {"triggered": True, "periods": 3, "status": "ok"}
        r = _rg(suggestions=sug, advice_evidence=ev, conclusions={"plateau": plateau})
        assert len(r["bullets"]) == 2
        assert r["bullets"][1]["source"] == "plateau"
        assert "平台期" in r["bullets"][1]["text"]
        # 再次调用不因已有平台期文案重复追加（模拟 suggestions 已含）
        sug2 = sug + [r["bullets"][1]["text"]]
        r2 = _rg(suggestions=sug2, advice_evidence=ev, conclusions={"plateau": plateau})
        assert sum(1 for b in r2["bullets"] if b["source"] == "plateau") == 0
        assert sum(1 for b in r2["bullets"] if "平台期" in b["text"]) == 1

    def test_title_stable(self):
        assert _rg(suggestions=["x"], advice_evidence=[{"ref": "advice"}])["title"] == "规则引擎智能指导"
