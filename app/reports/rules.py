# -*- coding: utf-8 -*-
"""报告层 · 规则引擎（指标→结论，证据可追溯）。

口径来源：工程化方案 D4/D4.1 + 决策记录 v2。
- 方向与阈值 SSOT（B4）：只读 config.mdc（键 = metric 名），不内联
- degrade 必填（B3）：含判定词的模板必须带降级模板（mdc_source 为空时"只描述不判定"）
- 禁语清单（D4）：规则产出文案时校验
- 样本门槛（A4）：窗口箭数不足或周报训练次数 <2 → 禁止判定词
- 可比性（C4）：跨场/跨期判定前校验距离/箭数/模式构成/平均风况
- 平台期（D7-C3）：连续 3 个同粒度窗口均"平稳" → plateau
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import EngineConfig, get_config

# 指标方向语义：+1 越大越好（avgScore）；-1 越小越好（mcrT/hrVolatility/dispersionMm）
# 判定词映射（方向无关，与参照系差值一致）

DIRECTION_LABEL = {1: "progress", -1: "regression"}
OPPOSITE_LABEL = {1: "regression", -1: "progress"}
STEADY = "steady"


@dataclass
class RuleSpec:
    """一条规则：metric + 判定 + 降级模板（B3 必填 degrade）。"""

    id: str
    metric: str
    template_progress: str
    template_regression: str
    template_steady: str
    template_degrade: str  # mdc_source 为空时使用（只描述，不判定）
    evidence_refs: list[str] = field(default_factory=list)


RULES: dict[str, RuleSpec] = {
    "avg_vs_anchor": RuleSpec(
        id="avg_vs_anchor",
        metric="avgScore",
        template_progress="平均环 {now} 较锚点（{ref_date} 采集）进步 {delta} 环",
        template_regression="平均环 {now} 较锚点（{ref_date} 采集）退步 {delta} 环",
        template_steady="平均环 {now} 与锚点（{ref_date} 采集）相比平稳（差 {delta} 环，未超最小可检测变化 MDC {mdc}）",
        template_degrade="平均环 {now}，实测较锚点（{ref_date} 采集）差 {delta} 环（MDC 口径待专家共识，未判定）",
        evidence_refs=["now", "ref", "delta", "ref_date", "mdc"],
    ),
    "mcr_vs_anchor": RuleSpec(
        id="mcr_vs_anchor",
        metric="mcrT",
        template_progress="撒放用时 {now}s，较锚点（{ref_date} 采集）缩短 {delta}s（撒放更干脆）",
        template_regression="撒放用时 {now}s，较锚点（{ref_date} 采集）延长 {delta}s",
        template_steady="撒放用时 {now}s，与锚点（{ref_date} 采集）相比平稳（差 {delta}s，未超 MDC {mdc}）",
        template_degrade="撒放用时 {now}s，实测较锚点（{ref_date} 采集）差 {delta}s（未判定）",
        evidence_refs=["now", "ref", "delta", "ref_date", "mdc"],
    ),
    "dispersion_vs_anchor": RuleSpec(
        id="dispersion_vs_anchor",
        metric="dispersionMm",
        template_progress="散布 {now}mm，较锚点（{ref_date} 采集）收窄 {delta}mm（弹着更集中）",
        template_regression="散布 {now}mm，较锚点（{ref_date} 采集）增大 {delta}mm",
        template_steady="散布 {now}mm，与锚点（{ref_date} 采集）相比平稳（差 {delta}mm，未超 MDC {mdc}）",
        template_degrade="散布 {now}mm，实测较锚点（{ref_date} 采集）差 {delta}mm（未判定）",
        evidence_refs=["now", "ref", "delta", "ref_date", "mdc"],
    ),
    "hr_volatility_vs_anchor": RuleSpec(
        id="hr_volatility_vs_anchor",
        metric="hrVolatility",
        template_progress="跨箭心率波动 {now}bpm，较锚点（{ref_date} 采集）回落 {delta}bpm（状态更平稳）",
        template_regression="跨箭心率波动 {now}bpm，较锚点（{ref_date} 采集）升高 {delta}bpm（波动加大，注意状态调节）",
        template_steady="跨箭心率波动 {now}bpm，与锚点（{ref_date} 采集）相比平稳（差 {delta}bpm，未超 MDC {mdc}）",
        template_degrade="跨箭心率波动 {now}bpm，实测较锚点（{ref_date} 采集）差 {delta}bpm（MDC 阈值待专家共识，未判定）",
        evidence_refs=["now", "ref", "delta", "ref_date", "mdc"],
    ),
}

# 参与 MDC 判定的指标必须在此列（与 config.mdc 键一致校验在 load 时完成）
JUDGE_METRICS = ("avgScore", "mcrT", "hrVolatility", "dispersionMm")


def validate_rules(cfg: EngineConfig) -> None:
    """加载时校验（B4/B3）：规则 metric 必须在 config.mdc 存在；degrade 非空。"""
    for rule in RULES.values():
        if rule.metric not in cfg.mdc:
            raise ValueError(f"规则 {rule.id} 的 metric={rule.metric} 不在 config.mdc（B4 SSOT）")
        if not rule.template_degrade.strip():
            raise ValueError(f"规则 {rule.id} 缺 degrade 模板（B3 必填）")


def _delta_now_ref(now: float, ref: float, direction: int) -> tuple[str, float]:
    """返回 (判定词, 绝对值差)。direction=+1 越大越好；-1 越小越好。"""
    raw = now - ref
    if direction == 1:
        if raw > 0:
            return "progress", abs(raw)
        if raw < 0:
            return "regression", abs(raw)
        return STEADY, 0.0
    # direction == -1：越小越好，now < ref = 进步
    if raw < 0:
        return "progress", abs(raw)
    if raw > 0:
        return "regression", abs(raw)
    return STEADY, 0.0


def judge_metric(cfg: EngineConfig, metric: str, now: float | None, ref: float | None) -> dict:
    """单指标 MDC 判定：返回 {judge, delta, mdc, degraded}。

    降级条件（C1）：mdc_source 为空 → degraded=True，只给差值不判定。
    样本量校验由调用方（window 层）负责；此处只判 MDC。
    """
    if now is None or ref is None:
        return {"judge": None, "delta": None, "mdc": None, "degraded": True,
                "reason": "缺测，不出结论"}
    spec = cfg.mdc[metric]
    if cfg.mdc_source is None:
        return {"judge": None, "delta": round(abs(now - ref), 3), "mdc": spec.threshold,
                "degraded": True, "reason": "mdc_source 为空，走降级模板"}
    if spec.threshold is None:
        return {"judge": None, "delta": round(abs(now - ref), 3), "mdc": None,
                "degraded": True, "reason": "MDC 阈值待专家共识（C2/M4.5）"}
    judge, delta = _delta_now_ref(now, ref, spec.direction)
    if delta < spec.threshold:
        judge = STEADY
    return {"judge": judge, "delta": round(delta, 3), "mdc": spec.threshold, "degraded": False}


def render_rule(cfg: EngineConfig, rule: RuleSpec, result: dict, vars_: dict) -> dict:
    """按判定结果选模板并渲染。返回 {conclusion, judge, degraded, delta, evidence}。"""
    judge = result["judge"]
    if result["degraded"] or judge is None:
        template = rule.template_degrade
    elif judge == "progress":
        template = rule.template_progress
    elif judge == "regression":
        template = rule.template_regression
    else:
        template = rule.template_steady
    text = template.format(**vars_)
    # 禁语校验（D4）：命中黑名单 → 抛错（报告不可生成，防硬编码归因句）
    for phrase in cfg.forbidden_phrases:
        if phrase in text:
            raise ValueError(f"规则 {rule.id} 文案命中禁语「{phrase}」")
    evidence = {"type": "calc", "ref": rule.id,
                "value": {r: vars_.get(r) for r in rule.evidence_refs if r in vars_}}
    return {"conclusion": text, "judge": judge, "degraded": result["degraded"],
            "delta": result["delta"], "evidence": evidence}


def sample_gate(cfg: EngineConfig, granularity: str, n_shots: int, n_sessions: int) -> bool:
    """样本门槛（A4 两级护栏）：达标返回 True，未达标禁止判定词。"""
    min_shots = cfg.sample_threshold.model_dump().get(granularity, 0)
    if n_shots < min_shots:
        return False
    if granularity == "weekly" and n_sessions < 2:
        return False  # 基础护栏：周报 ≥ 2 次训练
    return True
