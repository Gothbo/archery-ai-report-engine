# -*- coding: utf-8 -*-
"""报告层 · 生成器（组装：指标 + 规则 → 结构化报告 JSON）。

流程（记忆系统方案 §3.3）：
① 读记忆：profile / 最新锚点快照（MDC 参照系）+ 最新滚动快照（水平描述）/
   近期 notes（双视角过滤）/ 历史结论（同粒度最近 1 条）
② 计算指标（metrics，纯函数）
③ 规则出结论（锚点判 MDC + 滚动描述水平；可比性校验 C4；样本门槛 A4）
④ 结论回写记忆（judgement/attribution 分流）
⑤ 备注注入（双视角）
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from app.config import EngineConfig, get_config
from app.metrics.environment import wind_band_avg_scores, wind_band_counts
from app.metrics.performance import far_miss_rate, hit_rate, inner10_rate, total_score
from app.metrics.process import offset_mm
from app.metrics.vector import metric_vector
from app.reports import rules as R
from app.reports.window import window_bounds
from app.store.database import Database
from app.timeutil import iso_now_utc

logger = logging.getLogger("engine.reports.generator")

# 报告固定六段骨架（ADR-0003）：顺序即优先级。wind_bands 无风数据时并入 data_integrity。
SECTION_ORDER = ("window_score", "level", "data_integrity", "wind_bands", "sample_gate", "auxiliary")

_SCORE_TITLES = {"daily": "本场成绩", "weekly": "本周成绩", "monthly": "本月成绩",
                 "quarterly": "本季成绩", "yearly": "本年成绩"}


def _shots_in_window(db: Database, athlete_id: str, granularity: str, window_key: str,
                     session_id: str | None = None) -> list[dict]:
    """取窗口箭集：daily 用 session_id；其余用本地时间窗 [start, end) 过滤。"""
    if granularity == "daily":
        if not session_id:
            raise ValueError("daily 报告必须指定 session_id")
        rows = db.shots_of_session(session_id)
        return [dict(r) for r in rows]
    start_iso, end_iso = window_bounds(get_config(), granularity, window_key)
    rows = db.shots_in_window(athlete_id, start_iso, end_iso)
    return [dict(r) for r in rows]


def _session_ids_in_window(db: Database, athlete_id: str, granularity: str, window_key: str) -> list[str]:
    if granularity == "daily":
        return []
    start_iso, end_iso = window_bounds(get_config(), granularity, window_key)
    return db.session_ids_in_window(athlete_id, start_iso, end_iso)


def _calc_metrics(shots: list[dict]) -> dict:
    v = metric_vector(shots)
    scores = [s["score"] for s in shots]
    hits = [bool(s["hit"]) for s in shots]
    pairs = [(s["x_mm"], s["y_mm"]) for s in shots
             if s.get("x_mm") is not None and s.get("y_mm") is not None]
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return {
        "n_shots": len(shots),
        "avg_score": v.avg_score,
        "inner10_rate": round(inner10_rate(scores), 1),
        "far_miss_rate": round(far_miss_rate(scores), 1),
        "hit_rate": round(hit_rate(hits), 1),
        "total_score": total_score(scores),
        "mcr_t": v.mcr_t,
        "hr_volatility": v.hr_volatility,
        "dispersion_mm": v.dispersion_mm,
        "offset": offset_mm(xs, ys),
    }


def _comparability(db: Database, shots: list[dict], ref: dict) -> bool:
    """C4 可比性校验：窗口 session_dim 平均风况 + 锚点元数据。返回 True=可比。"""
    cfg = get_config()
    session_ids = {s["session_id"] for s in shots}
    if not session_ids:
        return False
    rows = db.sessions_by_ids(list(session_ids))
    dims = [dict(r) for r in rows]
    if not dims:
        return False
    avg_wind = sum(d["avg_wind"] for d in dims if d.get("avg_wind") is not None) / max(
        sum(1 for d in dims if d.get("avg_wind") is not None), 1)
    # 距离一致
    distances = {d.get("distance_m") for d in dims}
    ref_dist = ref.get("distance_m")
    if ref_dist is not None and len(distances - {ref_dist}) > 0:
        return False
    # 平均风况差异（经验阈值：>1.5 m/s 视为不可比，与风档边界一致）
    ref_wind = ref.get("avg_wind")
    if ref_wind is not None and abs(avg_wind - ref_wind) > 1.5:
        return False
    return True


def generate_report(db: Database, athlete_id: str, granularity: str, window_key: str,
                    session_id: str | None = None, view: str = "athlete", force: bool = False) -> dict:
    """生成（或读缓存）一份报告：缓存查找 → 清理旧窗口 → 组装 → 持久化。

    force=True 跳过缓存强制重生成（B10 支持 ?refresh=1）。
    """
    cfg = get_config()
    R.validate_rules(cfg)

    # 缓存查找（B10：key = granularity+window_key+口径版本）
    if not force:
        cached_id = db.get_cached_report(athlete_id, granularity, window_key, cfg.mdc_source)
        if cached_id:
            return {"report_id": cached_id, "cached": True}

    # 同窗口重生成：先清理旧缓存与旧结论记忆（必须在写新记忆之前，否则「近 3 期/近 4 周」
    # 判定会读到上一次重生成留下的重复行）
    db.purge_report_window(athlete_id, granularity, window_key, cfg.mdc_source)

    draft = build_report(db, athlete_id, granularity, window_key, session_id, view)
    persist_report(db, draft)
    logger.info("报告已生成 id=%s granularity=%s window=%s view=%s",
                draft.report["report_id"], granularity, window_key, view)
    return draft.report


@dataclass(frozen=True)
class ReportDraft:
    """报告组装结果（组装不写库）：待返回 JSON + 待落库结论。

    build_report（组装，纯读+计算）与 persist_report（持久化，写结论记忆+报告缓存）之间的
    seam：组装可单独测试/预览而不产生副作用。
    """

    report: dict
    conclusions: list[dict]


def build_report(db: Database, athlete_id: str, granularity: str, window_key: str,
                 session_id: str | None = None, view: str = "athlete") -> ReportDraft:
    """组装一份报告（纯读 + 计算）：读记忆/事实 → 指标 → 规则结论 → 结构化 JSON。

    不写库；结论与缓存由 persist_report 落库。
    """
    cfg = get_config()
    R.validate_rules(cfg)

    shots = _shots_in_window(db, athlete_id, granularity, window_key, session_id)
    session_ids = _session_ids_in_window(db, athlete_id, granularity, window_key)
    m = _calc_metrics(shots)

    profile = db.get_profile(athlete_id)
    anchor = db.latest_snapshot(athlete_id, "anchor")
    rolling = db.latest_snapshot(athlete_id, "rolling")
    anchor_dict = dict(anchor) if anchor else None
    rolling_dict = dict(rolling) if rolling else None

    # 样本门槛（A4）：不足 → 降级"样本不足，仅供参考"，禁止判定词
    gate_ok = R.sample_gate(cfg, granularity, m["n_shots"], len(session_ids))

    report_id = str(uuid.uuid4())

    conclusions: list[dict] = []

    # 固定六段骨架（ADR-0003）：顺序即优先级，任一段无数据也照常出现（缺失即标注）。
    # wind_bands 特例：无风数据时并入 data_integrity，不单独成段。
    by_key: dict[str, dict] = {}

    # ① window_score：本窗口成绩
    score_text = (f"平均环 {m['avg_score']}（n={m['n_shots']}），内十率 {m['inner10_rate']}%，"
                  f"远弹率 {m['far_miss_rate']}%，命中率 {m['hit_rate']}%")
    by_key["window_score"] = {
        "key": "window_score", "title": _SCORE_TITLES.get(granularity, "成绩"),
        "content": [score_text],
        "evidence": [{"type": "calc", "ref": "window_score", "value": {"n": m["n_shots"]}}],
    }

    # ② level：水平对比（有锚点且样本达标出方向判定；否则显式占位，不静默省略）
    level_lines: list[str] = []
    level_evidence: list[dict] = []
    if gate_ok and anchor_dict:
        comparable = _comparability(db, shots, anchor_dict)
        # 锚点日期优先取源场次（入队测试日），快照创建日仅兜底
        anchor_date = (anchor_dict["collected_at_utc"] or "")[:10]
        if anchor_dict.get("source_session_id"):
            src = db.session_of(anchor_dict["source_session_id"])
            if src and src["session_time_utc"]:
                anchor_date = (src["session_time_utc"] or "")[:10]
        for metric, rule in R.RULES.items():
            now_v = {"avgScore": m["avg_score"], "mcrT": m["mcr_t"],
                     "hrVolatility": m["hr_volatility"], "dispersionMm": m["dispersion_mm"]}[rule.metric]
            ref_v = {"avgScore": anchor_dict.get("avg_score"), "mcrT": anchor_dict.get("mcr_t"),
                     "hrVolatility": anchor_dict.get("hr_volatility"),
                     "dispersionMm": anchor_dict.get("dispersion_mm")}[rule.metric]
            if now_v is None or ref_v is None:
                continue  # 缺测不出结论（B11）
            res = R.judge_metric(cfg, rule.metric, now_v, ref_v)
            if not comparable:
                # 不可比：只描述不判定
                res["degraded"] = True
                res["judge"] = None
            rendered = R.render_rule(cfg, rule, res, {
                "now": now_v, "ref": ref_v, "delta": res["delta"] if res["delta"] is not None else "-",
                "ref_date": anchor_date, "mdc": res["mdc"] if res["mdc"] is not None else "-",
            })
            if rendered["judge"] is not None:
                conclusions.append({
                    "conclusion_key": rendered["judge"], "conclusion": rendered["conclusion"],
                    "judge_basis": f"anchor_id={anchor_dict['id']};mdc_source={cfg.mdc_source};window_shots={m['n_shots']}",
                    "delta_value": rendered["delta"], "evidence": str(rendered["evidence"]),
                })
            if not comparable:
                rendered["conclusion"] = "条件不同（距离/风况与锚点不可比），不做判定。"
            level_lines.append(rendered["conclusion"])
            level_evidence.append(rendered["evidence"])
    if not level_lines:
        level_lines = ["本窗口暂无可判定的水平对比（无锚点或样本不足）"]
    by_key["level"] = {"key": "level", "title": "水平对比",
                       "content": level_lines, "evidence": level_evidence}

    # ③ data_integrity：采样缺失显式标注（无风数据时风档并入此段）
    has_wind = any(s.get("wind_speed") is not None for s in shots)
    has_hr = any(s.get("hr") is not None for s in shots)
    integrity_lines: list[str] = []
    if m["n_shots"] == 0:
        integrity_lines.append("本窗口无箭（空窗口）")
    if not has_wind:
        integrity_lines.append("本窗口无风速数据，风档对照不可用")
    if not has_hr:
        integrity_lines.append("本窗口无心率数据，心率波动指标不可用")
    if not integrity_lines:
        integrity_lines.append("本窗口采样数据完整（风速 / 心率均有覆盖）")
    by_key["data_integrity"] = {
        "key": "data_integrity", "title": "数据完整性", "content": integrity_lines,
        "evidence": [{"type": "fact", "ref": "data_integrity",
                      "value": {"has_wind": has_wind, "has_hr": has_hr, "n_shots": m["n_shots"]}}],
    }

    # ④ wind_bands：有风数据时独立成段；无则并入 data_integrity（上一步已标注），不单独成段
    band_avg = wind_band_avg_scores(shots, cfg.wind_bands)
    band_counts = wind_band_counts(shots, cfg.wind_bands)
    if band_avg:
        band_names = ["[0,1.5)", "[1.5,2.0)", "[2.0,2.5)", "≥2.5"]
        band_lines = [f"{band_names[b]} 档：均环 {v}（n={band_counts.get(b, 0)}）"
                      for b, v in sorted(band_avg.items())]
        by_key["wind_bands"] = {"key": "wind_bands", "title": "风档对照", "content": band_lines,
                                "evidence": [{"type": "fact", "ref": "wind_band_avg", "value": band_avg}]}

    # ⑤ sample_gate：达标也显式出现，不达标出提示
    if gate_ok:
        by_key["sample_gate"] = {
            "key": "sample_gate", "title": "样本达标",
            "content": [f"样本达标（窗口箭数 {m['n_shots']}），本窗口结论可信"],
            "evidence": [{"type": "calc", "ref": "sample_gate",
                          "value": {"n": m["n_shots"], "ok": True}}],
        }
    else:
        by_key["sample_gate"] = {
            "key": "sample_gate", "title": "样本不足",
            "content": ["样本不足，仅供参考（窗口箭数/训练次数未达样本门槛，不出判定词）"],
            "evidence": [{"type": "calc", "ref": "sample_gate",
                          "value": {"n": m["n_shots"], "ok": False}}],
        }

    # ⑥ auxiliary：备注 + 平台期（有则出，无则显式标注）
    aux_lines: list[str] = []
    aux_evidence: list[dict] = []
    # 平台期识别（D7-C3）：连续 3 个同粒度窗口均"平稳" → plateau
    recent = [dict(r) for r in db.recent_judgements(athlete_id, granularity, 3)]
    if len(recent) == 3 and all(r["conclusion_key"] == "steady" for r in recent):
        plateau_text = ("近 3 期成绩均与锚点平稳（差值未超最小可检测变化），疑似进入平台期，"
                        "建议调整训练刺激（辅助判断，仅供参考）")
        aux_lines.append(plateau_text)
        aux_evidence.append({"type": "fact", "ref": "last_3_steady",
                             "value": [r["generated_at_utc"] for r in recent]})
        conclusions.append({"conclusion_key": "plateau", "conclusion": plateau_text,
                            "judge_basis": None, "delta_value": None, "evidence": ""})
    # 备注注入（双视角，A2/D9）
    from app.memory.notes import list_notes_for_view
    notes = list_notes_for_view(db, athlete_id, view)
    if notes:
        note_lines = [f"[{n['note_type']}] {n['content']}（记录人：{'教练' if n['author_role'] == 'coach' else '运动员'}）"
                      for n in notes[:5]]
        aux_lines.extend(note_lines)
        aux_evidence.append({"type": "fact", "ref": "notes", "value": len(notes)})
    if not aux_lines:
        aux_lines = ["本窗口暂无辅助信息（无备注、无平台期提示）"]
    by_key["auxiliary"] = {"key": "auxiliary", "title": "辅助信息",
                           "content": aux_lines, "evidence": aux_evidence}

    sections = [by_key[k] for k in SECTION_ORDER if k in by_key]

    report = {
        "report_id": report_id,
        "granularity": granularity,
        "window_key": window_key,
        "athlete": {"id": athlete_id, "name": profile["name"] if profile else "运动员"},
        "view": view,
        "cached": False,
        "generated_at_utc": iso_now_utc(),
        "sections": sections,
        "suggestions": [],
        "coach_extra": {"warnings": [], "load": {}},
        "anchor_rebuild_hint": _anchor_rebuild_hint(db, athlete_id),
    }
    if rolling_dict:
        report["coach_extra"]["rolling_baseline"] = {
            "n_shots": rolling_dict["n_shots"], "avg_score": rolling_dict.get("avg_score"),
            "collected_at_utc": rolling_dict["collected_at_utc"],
        }
    return ReportDraft(report=report, conclusions=conclusions)


def persist_report(db: Database, draft: ReportDraft) -> None:
    """把组装结果落库：结论回写记忆（④：judgement 类供历史引用）+ 报告缓存写入（B10）。"""
    from app.memory.memories import record_conclusion

    report = draft.report
    athlete_id = report["athlete"]["id"]
    for c in draft.conclusions:
        record_conclusion(
            db, athlete_id=athlete_id, report_id=report["report_id"],
            granularity=report["granularity"], conclusion_key=c["conclusion_key"],
            conclusion=c["conclusion"], judge_basis=c["judge_basis"],
            delta_value=c["delta_value"], evidence=c["evidence"])
    db.put_cached_report(report["report_id"], athlete_id, report["granularity"],
                         report["window_key"], get_config().mdc_source)


def _anchor_rebuild_hint(db: Database, athlete_id: str) -> dict:
    """锚点重建触发④（连续 4 周提升均超 MDC）：周报生成时检查（B3：降级期跳过）。"""
    cfg = get_config()
    if cfg.mdc_source is None:
        return {"hint": False, "reason": "mdc_source 为空（降级期），跳过触发④"}
    min_weeks = cfg.memory.anchor_rebuild_policy.four_week_min_weeks
    rows = db.recent_judgements(athlete_id, "weekly", min_weeks, with_delta=True)
    if len(rows) < min_weeks:
        return {"hint": False, "reason": f"周报判定不足 {min_weeks} 条"}
    metric_ok = all(r["conclusion_key"] in ("progress", "regression") for r in rows)
    if not metric_ok:
        return {"hint": False, "reason": "近 4 周存在非判定或平稳结论"}
    return {"hint": True, "reason": "连续 4 周方向判定均超 MDC，建议重建锚点"}
