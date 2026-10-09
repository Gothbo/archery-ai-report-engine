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
from datetime import datetime, timezone

from app.config import EngineConfig, get_config
from app.metrics.environment import wind_band_avg_scores, wind_band_counts
from app.metrics.performance import avg_score, far_miss_rate, hit_rate, inner10_rate, total_score
from app.metrics.physiology import hr_volatility
from app.metrics.process import dispersion_mm, mean_mcr_t, offset_mm
from app.reports import rules as R
from app.reports.window import window_bounds
from app.store.database import Database

logger = logging.getLogger("engine.reports.generator")


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _shots_in_window(db: Database, athlete_id: str, granularity: str, window_key: str,
                     session_id: str | None = None) -> list[dict]:
    """取窗口箭集：daily 用 session_id；其余用本地时间窗 [start, end) 过滤。"""
    if granularity == "daily":
        if not session_id:
            raise ValueError("daily 报告必须指定 session_id")
        rows = db.shots_of_session(session_id)
        return [dict(r) for r in rows]
    start_iso, end_iso = window_bounds(get_config(), granularity, window_key)
    rows = db.query(
        """SELECT * FROM shot_fact
           WHERE athlete_id=? AND shot_time_utc>=? AND shot_time_utc<? ORDER BY shot_time_utc""",
        (athlete_id, start_iso, end_iso),
    )
    return [dict(r) for r in rows]


def _session_ids_in_window(db: Database, athlete_id: str, granularity: str, window_key: str) -> list[str]:
    if granularity == "daily":
        return []
    start_iso, end_iso = window_bounds(get_config(), granularity, window_key)
    rows = db.query(
        """SELECT DISTINCT session_id FROM session_dim
           WHERE athlete_id=? AND session_time_utc>=? AND session_time_utc<?""",
        (athlete_id, start_iso, end_iso),
    )
    return [r["session_id"] for r in rows]


def _calc_metrics(shots: list[dict]) -> dict:
    scores = [s["score"] for s in shots]
    hits = [bool(s["hit"]) for s in shots]
    mcr_vals = [s["mcr_t"] for s in shots if s.get("mcr_t") is not None]
    hr_vals = [s["hr"] for s in shots if s.get("hr") is not None]
    xs = [s["x_mm"] for s in shots if s.get("x_mm") is not None and s.get("y_mm") is not None]
    ys = [s["y_mm"] for s in shots if s.get("x_mm") is not None and s.get("y_mm") is not None]
    return {
        "n_shots": len(shots),
        "avg_score": round(avg_score(scores), 2),
        "inner10_rate": round(inner10_rate(scores), 1),
        "far_miss_rate": round(far_miss_rate(scores), 1),
        "hit_rate": round(hit_rate(hits), 1),
        "total_score": total_score(scores),
        "mcr_t": round(mean_mcr_t(mcr_vals), 3) if mcr_vals else None,
        "hr_volatility": round(hr_volatility(hr_vals), 1) if hr_vals else None,
        "dispersion_mm": round(dispersion_mm(xs, ys), 1) if len(xs) >= 2 else None,
        "offset": offset_mm(xs, ys),
    }


def _comparability(db: Database, shots: list[dict], ref: dict) -> bool:
    """C4 可比性校验：窗口 session_dim 平均风况 + 锚点元数据。返回 True=可比。"""
    cfg = get_config()
    session_ids = {s["session_id"] for s in shots}
    if not session_ids:
        return False
    rows = db.query(
        f"SELECT * FROM session_dim WHERE session_id IN ({','.join('?' * len(session_ids))})",
        tuple(session_ids),
    )
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
    """生成（或读缓存）一份报告。返回结构化报告 JSON。

    force=True 跳过缓存强制重生成（B10 支持 ?refresh=1）。
    """
    cfg = get_config()
    R.validate_rules(cfg)

    # 缓存查找（B10：key = granularity+window_key+口径版本）
    cached_id = None if force else db.get_cached_report(
        athlete_id, granularity, window_key, cfg.mdc_source)
    if cached_id and not force:
        return {"report_id": cached_id, "cached": True}

    # 同窗口重生成：先清理旧缓存与旧结论记忆（必须在写新记忆之前，否则「近 3 期/近 4 周」
    # 判定会读到上一次重生成留下的重复行）
    db.purge_report_window(athlete_id, granularity, window_key, cfg.mdc_source)

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

    sections: list[dict] = []
    conclusions: list[dict] = []

    # 今日/窗口成绩
    score_text = f"平均环 {m['avg_score']}（n={m['n_shots']}），内十率 {m['inner10_rate']}%，远弹率 {m['far_miss_rate']}%，命中率 {m['hit_rate']}%"
    if granularity == "daily":
        score_title = "本场成绩"
    elif granularity == "weekly":
        score_title = "本周成绩"
    elif granularity == "monthly":
        score_title = "本月成绩"
    elif granularity == "quarterly":
        score_title = "本季成绩"
    else:
        score_title = "本年成绩"
    sections.append({
        "key": "window_score", "title": score_title, "content": [score_text],
        "evidence": [{"type": "calc", "ref": "window_score", "value": {"n": m["n_shots"]}}],
    })

    # 锚点 MDC 判定（主判定，仅当锚点存在且样本达标）
    if gate_ok and anchor_dict:
        comparable = _comparability(db, shots, anchor_dict)
        # 锚点日期优先取源场次（入队测试日），快照创建日仅兜底
        anchor_date = (anchor_dict["collected_at_utc"] or "")[:10]
        if anchor_dict.get("source_session_id"):
            src = db.query("SELECT session_time_utc FROM session_dim WHERE session_id=?",
                           (anchor_dict["source_session_id"],))
            if src and src[0]["session_time_utc"]:
                anchor_date = (src[0]["session_time_utc"] or "")[:10]
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
                    "key": rule.id, "judge": rendered["judge"], "delta": rendered["delta"],
                    "conclusion": rendered["conclusion"], "metric": rule.metric,
                    "judge_basis": f"anchor_id={anchor_dict['id']};mdc_source={cfg.mdc_source};window_shots={m['n_shots']}",
                    "evidence": rendered["evidence"],
                })
            if not comparable:
                rendered["conclusion"] = "条件不同（距离/风况与锚点不可比），不做判定。"
            sections.append({"key": rule.id, "title": _metric_title(rule.metric),
                             "content": [rendered["conclusion"]],
                             "evidence": [rendered["evidence"]]})
    elif not gate_ok:
        sections.append({"key": "sample_gate", "title": "样本不足", "content": [
            "样本不足，仅供参考（窗口箭数/训练次数未达样本门槛，不出判定词）"]})

    # 风档分层（C4）：展示各档均环
    band_avg = wind_band_avg_scores(shots)
    band_counts = wind_band_counts(shots)
    if band_avg:
        band_names = ["[0,1.5)", "[1.5,2.0)", "[2.0,2.5)", "≥2.5"]
        band_lines = [f"{band_names[b]} 档：均环 {v}（n={band_counts.get(b, 0)}）"
                      for b, v in sorted(band_avg.items())]
        sections.append({"key": "wind_bands", "title": "风档对照", "content": band_lines,
                         "evidence": [{"type": "fact", "ref": "wind_band_avg", "value": band_avg}]})

    # 平台期识别（D7-C3）：连续 3 个同粒度窗口均"平稳" → plateau（P1 标记为辅助）
    recent = [dict(r) for r in db.query(
        """SELECT * FROM report_memories WHERE athlete_id=? AND granularity=?
             AND conclusion_type='judgement' ORDER BY generated_at_utc DESC LIMIT 3""",
        (athlete_id, granularity),
    )]
    if len(recent) == 3 and all(r["conclusion_key"] == "steady" for r in recent):
        from app.memory.memories import record_conclusion
        plateau_text = ("近 3 期成绩均与锚点平稳（差值未超最小可检测变化），疑似进入平台期，"
                        "建议调整训练刺激（辅助判断，仅供参考）")
        sections.append({"key": "plateau", "title": "平台期提示", "content": [plateau_text],
                         "evidence": [{"type": "fact", "ref": "last_3_steady",
                                       "value": [r["generated_at_utc"] for r in recent]}]})
        record_conclusion(db, athlete_id=athlete_id, report_id=report_id, granularity=granularity,
                          conclusion_key="plateau", conclusion=plateau_text, judge_basis=None,
                          delta_value=None, evidence="")

    # 备注注入（双视角，A2/D9）
    from app.memory.notes import list_notes_for_view
    notes = list_notes_for_view(db, athlete_id, view)
    if notes:
        note_lines = [f"[{n['note_type']}] {n['content']}（记录人：{'教练' if n['author_role'] == 'coach' else '运动员'}）"
                      for n in notes[:5]]
        sections.append({"key": "notes", "title": "近期备注", "content": note_lines,
                         "evidence": [{"type": "fact", "ref": "notes", "value": len(notes)}]})

    report = {
        "report_id": report_id,
        "granularity": granularity,
        "window_key": window_key,
        "athlete": {"id": athlete_id, "name": profile["name"] if profile else "运动员"},
        "view": view,
        "cached": False,
        "generated_at_utc": _now_utc(),
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

    # 结论回写记忆（④：judgement 类落库供历史引用）
    for c in conclusions:
        from app.memory.memories import record_conclusion
        record_conclusion(
            db, athlete_id=athlete_id, report_id=report_id, granularity=granularity,
            conclusion_key=c["judge"], conclusion=c["conclusion"], judge_basis=c["judge_basis"],
            delta_value=c["delta"], evidence=str(c["evidence"]))

    db.put_cached_report(report_id, athlete_id, granularity, window_key, cfg.mdc_source)
    logger.info("报告已生成 id=%s granularity=%s window=%s view=%s", report_id, granularity, window_key, view)
    return report


def _metric_title(metric: str) -> str:
    return {"avgScore": "平均环 vs 锚点", "mcrT": "撒放用时 vs 锚点",
            "hrVolatility": "心率波动 vs 锚点", "dispersionMm": "散布 vs 锚点"}[metric]


def _anchor_rebuild_hint(db: Database, athlete_id: str) -> dict:
    """锚点重建触发④（连续 4 周提升均超 MDC）：周报生成时检查（B3：降级期跳过）。"""
    cfg = get_config()
    if cfg.mdc_source is None:
        return {"hint": False, "reason": "mdc_source 为空（降级期），跳过触发④"}
    min_weeks = cfg.memory.anchor_rebuild_policy.four_week_min_weeks
    rows = db.query(
        """SELECT * FROM report_memories WHERE athlete_id=? AND granularity='weekly'
             AND conclusion_type='judgement' AND delta_value IS NOT NULL
           ORDER BY generated_at_utc DESC LIMIT ?""",
        (athlete_id, min_weeks),
    )
    if len(rows) < min_weeks:
        return {"hint": False, "reason": f"周报判定不足 {min_weeks} 条"}
    metric_ok = all(r["conclusion_key"] in ("progress", "regression") for r in rows)
    if not metric_ok:
        return {"hint": False, "reason": "近 4 周存在非判定或平稳结论"}
    return {"hint": True, "reason": "连续 4 周方向判定均超 MDC，建议重建锚点"}
