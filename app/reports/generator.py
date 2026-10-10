# -*- coding: utf-8 -*-
"""报告层 · 生成器（组装：指标 + 规则 → 结构化报告 JSON）。

流程（记忆系统方案 §3.3）：
① 读记忆：profile / 最新锚点快照（MDC 参照系）+ 窗口前滚动基线（从事实层按窗口起点
   截断计算，非导入时点快照，避免历史窗口拿「当前水平」当参照）/
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
from app.metrics.performance import (
    effective_avg_score,
    far_miss_rate,
    hit_rate,
    inner10_rate,
    miss_rate,
    total_score,
)
from app.metrics.process import offset_mm
from app.metrics.vector import metric_vector
from app.reports import advice as ADV
from app.reports import quality as Q
from app.reports import rules as R
from app.reports.window import window_bounds
from app.store.database import Database
from app.timeutil import iso_now_utc

logger = logging.getLogger("engine.reports.generator")

# 报告固定六段骨架（ADR-0003）：顺序即优先级。wind_bands 无风数据时并入 data_integrity。
SECTION_ORDER = ("window_score", "level", "data_integrity", "wind_bands", "sample_gate", "auxiliary")

# 降级期提示（ADR-0002）：显式声明不出方向判定，避免「无结论」被误读为「有结论」
DEGRADE_HINT = "降级期，暂无进步/退步判定（MDC 阈值口径试行中，本段只描述水平、不判定方向）"
# 试行口径提示（B13）：判定已解锁但为草案值，须标注可撤回
TRIAL_HINT = "（变化判定口径 {src}，试行中：方向结论为初步判定，待专家签署后定稿）"
# 无锚点提示（P1）：正文只说明缺锚点导致无方向判定（信息），「建立锚点」动作在 coach_extra（ADR-0005）
NO_ANCHOR_HINT = "当前无锚点：锚点是方向判定的参照系，尚无锚点则本窗口不出方向判定"

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


def _primary_bow(shots: list[dict]) -> tuple[str | None, list[tuple[str, int]]]:
    """主弓种（箭数最多）+ 其余弓种箭数，供窗口按主弓种聚合（ADR-0004）。

    箭数并列时按弓种名升序取首个，保证同一窗口跨次生成结果一致；无箭返回 (None, [])。
    """
    counts: dict[str, int] = {}
    for s in shots:
        bow = s.get("bow_type")
        if bow:
            counts[bow] = counts.get(bow, 0) + 1
    if not counts:
        return None, []
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[0][0], ranked[1:]


def _window_range(db: Database, granularity: str, window_key: str,
                  session_id: str | None) -> tuple[str | None, str | None]:
    """窗口时间范围（UTC ISO）：daily 取场次时间（窗口退化为该时刻，start == end）；其余取窗口 [start, end) 边界（T5/D8）。"""
    if granularity == "daily":
        row = db.session_of(session_id) if session_id else None
        t = row["session_time_utc"] if row else None
        return t, t
    return window_bounds(get_config(), granularity, window_key)


def _rolling_baseline_before(db: Database, athlete_id: str, bow_type: str,
                             before_iso: str | None, n: int) -> dict | None:
    """窗口前近 N 支记分箭的滚动基线（时间语义：以窗口起点为界）。

    与记忆层「最新 N 箭」快照（导入时点口径，供档案视图）不同：报告用的基线必须相对窗口，
    否则历史窗口会拿「当前水平」当参照。不含窗口内箭（避免自比较），无先前箭则无基线。
    """
    if not before_iso:
        return None
    rows = db.recent_scoring_shots_before(athlete_id, bow_type, before_iso, n)
    shots = [dict(r) for r in rows]
    if not shots:
        return None
    v = metric_vector(shots)
    return {"n_shots": len(shots), "avg_score": v.avg_score,
            "collected_at_utc": shots[0]["shot_time_utc"]}


def _rolling_level_line(rolling_dict: dict, window_avg: float) -> tuple[str, dict]:
    """窗口前滚动基线的水平描述行 + evidence（只描述、不判定）。"""
    base_avg = rolling_dict["avg_score"]
    delta = round(window_avg - base_avg, 2)
    line = (f"滚动基线（窗口前近 {rolling_dict['n_shots']} 箭，"
            f"截至 {(rolling_dict.get('collected_at_utc') or '')[:10]}）均环 {base_avg:.2f}，"
            f"本窗口均环 {window_avg:.2f}，差值 {delta:+.2f} 环（水平描述，非判定）")
    ev = {"type": "fact", "ref": "rolling_baseline",
          "value": {"n_shots": rolling_dict["n_shots"], "avg_score": base_avg,
                    "delta_vs_window": delta}}
    return line, ev


def _calc_metrics(shots: list[dict]) -> dict:
    v = metric_vector(shots)
    scores = [s["score"] for s in shots]
    hits = [bool(s["hit"]) for s in shots]
    eff_avg = effective_avg_score(scores)
    pairs = [(s["x_mm"], s["y_mm"]) for s in shots
             if s.get("x_mm") is not None and s.get("y_mm") is not None]
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return {
        "n_shots": len(shots),
        "avg_score": v.avg_score,
        "effective_avg_score": round(eff_avg, 2) if eff_avg is not None else None,
        "miss_rate": round(miss_rate(scores), 1),
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


def _report_cache_version(cfg: EngineConfig) -> str:
    """报告缓存口径键（B10/B12）：MDC 阈值来源 + 报告口径版本，任一变更即作废旧缓存。

    与 mdc_source 解耦：口径升级只递增 report_caliber_version，不改变 mdc_source（降级期）。
    """
    return f"mdc={cfg.mdc_source or 'empty'};caliber={cfg.report_caliber_version}"


def generate_report(db: Database, athlete_id: str, granularity: str, window_key: str,
                    session_id: str | None = None, view: str = "athlete", force: bool = False) -> dict:
    """生成（或读缓存）一份报告：缓存查找 → 清理旧窗口 → 组装 → 持久化。

    force=True 跳过缓存强制重生成（B10 支持 ?refresh=1）。
    """
    cfg = get_config()
    R.validate_rules(cfg)
    cache_version = _report_cache_version(cfg)

    # 缓存查找（B10：key = granularity+window_key+口径版本）
    if not force:
        cached_id = db.get_cached_report(athlete_id, granularity, window_key, cache_version)
        if cached_id:
            return {"report_id": cached_id, "cached": True}

    # 同窗口重生成：先清理旧缓存与旧结论记忆（必须在写新记忆之前，否则「近 3 期/近 4 周」
    # 判定会读到上一次重生成留下的重复行）
    db.purge_report_window(athlete_id, granularity, window_key, cache_version)

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
    # 主弓种聚合（ADR-0004）：指标只算主弓种箭，其余弓种在 data_integrity 标注不计入
    primary_bow, other_bows = _primary_bow(shots)
    if primary_bow is not None:
        shots = [s for s in shots if s.get("bow_type") == primary_bow]
    # 训练次数按主弓种过滤：仅统计贡献了主弓种箭的场次，非主弓种场次不虚增样本门槛
    session_ids = sorted({s["session_id"] for s in shots})
    m = _calc_metrics(shots)
    # 空窗口（n=0）：avg_score([])==0.0、各比例恒为 0，直接呈现会像「测出来的 0」；
    # 三处受影响的段落（成绩 / 水平 / 样本）统一改显式缺失说明（ADR-0003：缺失即标注）
    is_empty = m["n_shots"] == 0
    window_start, window_end = _window_range(db, granularity, window_key, session_id)

    profile = db.get_profile(athlete_id)
    # 锚点按主弓种取（ADR-0004：不同项目基线不混用）；滚动基线按窗口起点截断（时间语义修复）
    anchor = db.latest_snapshot(athlete_id, "anchor", primary_bow)
    rolling_dict = (_rolling_baseline_before(db, athlete_id, primary_bow, window_start,
                                             cfg.rolling_window_shots)
                    if primary_bow else None)
    anchor_dict = dict(anchor) if anchor else None

    # 样本门槛（A4）：不足 → 降级"样本不足，仅供参考"，禁止判定词
    gate_ok = R.sample_gate(cfg, granularity, m["n_shots"], len(session_ids))

    # 数据质量护栏（P0）：不可能分布 / 过小样本显式标注「数据存疑」；
    # 结构矛盾时抑制方向判定（data_ok=False），避免用不可信数据出结论误导教练
    dq = Q.assess_quality(m, cfg.data_quality)
    data_ok = not dq["block_judgement"]

    report_id = str(uuid.uuid4())

    conclusions: list[dict] = []

    # 固定六段骨架（ADR-0003）：顺序即优先级，任一段无数据也照常出现（缺失即标注）。
    # wind_bands 特例：无风数据时并入 data_integrity，不单独成段。
    by_key: dict[str, dict] = {}

    # ① window_score：本窗口成绩（双口径：含脱靶均环 + 有效箭均环 + 脱靶率，ADR-0001）
    eff = m["effective_avg_score"]
    eff_txt = f"{eff:.2f}" if eff is not None else "—"
    if is_empty:
        # 空窗口不输出 0.00/0.0% 占位（会被读成「打出的 0 环」），改显式缺失说明
        score_content = ["本窗口无箭，无成绩可统计（空窗口）"]
    else:
        score_content = [
            f"含脱靶均环 {m['avg_score']:.2f}，有效箭均环 {eff_txt}，脱靶率 {m['miss_rate']:.1f}%，"
            f"内十率 {m['inner10_rate']:.1f}%，远弹率 {m['far_miss_rate']:.1f}%，"
            f"命中率 {m['hit_rate']:.1f}%（n={m['n_shots']}）"
        ]
    by_key["window_score"] = {
        "key": "window_score", "title": _SCORE_TITLES.get(granularity, "成绩"),
        "content": score_content,
        "evidence": [{"type": "calc", "ref": "window_score", "value": {"n": m["n_shots"]}}],
    }

    # ② level：水平对比（有锚点且样本达标出方向判定；降级期出降级提示 + 滚动基线描述；
    # 否则显式占位，不静默省略）
    level_lines: list[str] = []
    level_evidence: list[dict] = []
    if gate_ok and data_ok and anchor_dict:
        # 降级期（mdc_source 为空）仍走此分支：方向判定由 rules.judge_metric 统一降级（B3 SSOT），
        # 此处只渲染"只描述不判定"的 degrade 模板，故降级期不会出现方向判定词。
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
                res["tier"] = None
            rendered = R.render_rule(cfg, rule, res, {
                "now": now_v, "ref": ref_v, "delta": res["delta"] if res["delta"] is not None else "-",
                "delta_signed": f"{now_v - ref_v:+.2f}",
                "ref_date": anchor_date, "mdc": res["mdc"] if res["mdc"] is not None else "-",
                "mdc95": res["mdc"] if res["mdc"] is not None else "-",
                "te": res.get("te") if res.get("te") is not None else "-",
                "swc": res.get("swc") if res.get("swc") is not None else "-",
            })
            if rendered["judge"] is not None:
                # 参考档（方向提示，未达强断言门槛）以 *_ref 键落库：与强断言方向区分，不触发锚点重建
                key = f"{rendered['judge']}_ref" if rendered["tier"] == R.REFERENCE else rendered["judge"]
                conclusions.append({
                    "conclusion_key": key, "conclusion": rendered["conclusion"],
                    "judge_basis": (f"anchor_id={anchor_dict['id']};mdc_source={cfg.mdc_source};"
                                    f"tier={rendered['tier']};window_shots={m['n_shots']}"),
                    "delta_value": rendered["delta"], "evidence": str(rendered["evidence"]),
                })
            if not comparable:
                rendered["conclusion"] = "条件不同（距离/风况与锚点不可比），不做判定。"
            level_lines.append(rendered["conclusion"])
            level_evidence.append(rendered["evidence"])
    if is_empty:
        # 空窗口：无箭即无水平可对比。不渲染滚动基线行（其含「本窗口均环 0.00」会误导），
        # 也不追加「试行中」口径标注（本窗口未产生任何方向结论）
        if not level_lines:
            level_lines.append("本窗口无箭，无水平可对比")
    elif cfg.mdc_source is None:
        # 降级期（ADR-0002）：显式声明不出方向判定，改用滚动基线描述水平（只描述、不判定）
        level_lines.insert(0, DEGRADE_HINT)
        if rolling_dict and rolling_dict.get("avg_score") is not None:
            line, ev = _rolling_level_line(rolling_dict, m["avg_score"])
            level_lines.append(line)
            level_evidence.append(ev)
        if len(level_lines) == 1:
            level_lines.append("本窗口前无记分箭、无滚动基线，暂无可描述的水平")
    elif not level_lines:
        if not data_ok:
            # 数据质量护栏（P0）：分布存在结构性矛盾 → 抑制方向判定并显式说明原因
            level_lines = ["数据存疑：分布存在结构性矛盾，本窗口不出方向判定"]
        elif not gate_ok:
            # 样本不足：不引导建立锚点（建立后仍需样本达标才判定），显式说明并改述滚动基线
            level_lines.append("样本不足，本窗口不出方向判定（待样本达标后再判定方向）")
            if rolling_dict and rolling_dict.get("avg_score") is not None:
                line, ev = _rolling_level_line(rolling_dict, m["avg_score"])
                level_lines.append(line)
                level_evidence.append(ev)
        else:
            # 样本达标、数据可信：无锚点则显式说明缺锚点导致无方向判定（不静默省略）
            if primary_bow is not None and anchor_dict is None:
                # 无锚点引导（P1）：正文说明缺锚点；建立动作在 coach_extra（ADR-0005）
                level_lines.append(NO_ANCHOR_HINT)
            if rolling_dict and rolling_dict.get("avg_score") is not None:
                # 改述窗口前滚动基线水平（只描述、不判定）
                line, ev = _rolling_level_line(rolling_dict, m["avg_score"])
                level_lines.append(line)
                level_evidence.append(ev)
            if not level_lines:
                level_lines = ["本窗口暂无可判定的水平对比（指标缺测或样本不足）"]
    elif cfg.change_caliber_trial:
        # 试行口径（B13）：判定已解锁但为草案值，末行标注口径版本与「试行中」
        level_lines.append(TRIAL_HINT.format(src=cfg.mdc_source))
    by_key["level"] = {"key": "level", "title": "水平对比",
                       "content": level_lines, "evidence": level_evidence}

    # ③ data_integrity：采样缺失 + 数据质量存疑显式标注（无风数据时风档并入此段）
    has_wind = any(s.get("wind_speed") is not None for s in shots)
    has_hr = any(s.get("hr") is not None for s in shots)
    integrity_lines: list[str] = []
    if m["n_shots"] == 0:
        integrity_lines.append("本窗口无箭（空窗口）")
    if not has_wind:
        integrity_lines.append("本窗口无风速数据，风档对照不可用")
    if not has_hr:
        integrity_lines.append("本窗口无心率数据，心率波动指标不可用")
    if has_wind and has_hr:
        integrity_lines.append("本窗口采样数据完整（风速 / 心率均有覆盖）")
    # 数据质量存疑（P0）：不可能分布 / 过小样本显式标注；结构矛盾时说明已抑制方向判定
    for reason in dq["reasons"]:
        integrity_lines.append(f"数据存疑：{reason}")
    if dq["block_judgement"]:
        integrity_lines.append("数据存疑：分布存在结构性矛盾，已抑制方向判定")
    # 其余弓种显式标注不计入（ADR-0004）
    for bow, n in other_bows:
        integrity_lines.append(f"另有 {n} 箭为 {bow} 弓种，未计入")
    by_key["data_integrity"] = {
        "key": "data_integrity", "title": "数据完整性", "content": integrity_lines,
        "evidence": [{"type": "fact", "ref": "data_integrity",
                      "value": {"has_wind": has_wind, "has_hr": has_hr, "n_shots": m["n_shots"],
                                "quality_suspect": dq["suspect"],
                                "block_judgement": dq["block_judgement"]}}],
    }

    # ④ wind_bands：有风数据时独立成段；无则并入 data_integrity（上一步已标注），不单独成段
    band_avg = wind_band_avg_scores(shots, cfg.wind_bands)
    band_counts = wind_band_counts(shots, cfg.wind_bands)
    if band_avg:
        band_names = ["[0,1.5)", "[1.5,2.0)", "[2.0,2.5)", "≥2.5"]
        band_lines = []
        for b, v in sorted(band_avg.items()):
            n = band_counts.get(b, 0)
            # 样本偏少的档均环不可靠：与建议层比较门槛（advice.min_band_shots）一致显式标注，
            # 避免小样本噪声被读成「该风况下更好 / 更差」
            tail = (f"n={n}，样本偏少未参与风档比较"
                    if n < cfg.advice.min_band_shots else f"n={n}")
            band_lines.append(f"{band_names[b]} 档：均环 {v}（{tail}）")
        by_key["wind_bands"] = {"key": "wind_bands", "title": "风档对照", "content": band_lines,
                                "evidence": [{"type": "fact", "ref": "wind_band_avg", "value": band_avg}]}

    # ⑤ sample_gate：达标也显式出现，不达标出提示
    if is_empty:
        # 空窗口：门槛无从谈起（无箭可数），不写成「样本不足，仅供参考」（会暗示有内容可参考）
        by_key["sample_gate"] = {
            "key": "sample_gate", "title": "无样本",
            "content": ["本窗口无箭，样本门槛不适用"],
            "evidence": [{"type": "calc", "ref": "sample_gate",
                          "value": {"n": 0, "ok": False}}],
        }
    elif gate_ok:
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

    # 建议层（P0）：由判断信号（成绩结构/风况/趋势）产出确定性训练建议，
    # 填充 report.suggestions（前端已消费该字段）；每条建议带 evidence 溯源（仅教练附注区）
    adv = ADV.build_advice(
        m, conclusion_keys=[c["conclusion_key"] for c in conclusions],
        wind_band_avg=band_avg, wind_band_counts=band_counts, dq=dq, gate_ok=gate_ok,
        cfg=cfg)

    # 正文与运维分离（ADR-0005）：sections 只含 key/title/content，两视图内容一致
    # （备注按视图过滤是既定例外）；运维字段（evidence 明细、anchor_rebuild_hint）仅 coach 返回。
    evidence_by_key: dict[str, list[dict]] = {}
    sections: list[dict] = []
    for k in SECTION_ORDER:
        if k not in by_key:
            continue
        sec = by_key[k]
        evidence_by_key[k] = sec.get("evidence", [])
        sections.append({"key": sec["key"], "title": sec["title"], "content": sec["content"]})

    coach_extra: dict = {"warnings": [], "load": {}}
    if rolling_dict:
        coach_extra["rolling_baseline"] = {
            "n_shots": rolling_dict["n_shots"], "avg_score": rolling_dict.get("avg_score"),
            "collected_at_utc": rolling_dict["collected_at_utc"],
        }
    if view == "coach":
        coach_extra["evidence"] = evidence_by_key
        coach_extra["advice_evidence"] = adv["evidence"]
        coach_extra["anchor_rebuild_hint"] = _anchor_rebuild_hint(db, athlete_id)
        coach_extra["anchor_setup"] = _anchor_setup_hint(
            db, athlete_id, primary_bow, window_start, anchor_dict,
            gate_ok=gate_ok, data_ok=data_ok)

    report = {
        "report_id": report_id,
        "granularity": granularity,
        "window_key": window_key,
        "window_start": window_start,
        "window_end": window_end,
        "athlete": {"id": athlete_id, "name": profile["name"] if profile else "运动员"},
        "view": view,
        "cached": False,
        "generated_at_utc": iso_now_utc(),
        "sections": sections,
        "suggestions": adv["suggestions"],
        "coach_extra": coach_extra,
        "bow_type": primary_bow,
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
                         report["window_key"], _report_cache_version(get_config()))


def _anchor_setup_hint(db: Database, athlete_id: str, primary_bow: str | None,
                       window_start: str | None, anchor_dict: dict | None,
                       *, gate_ok: bool, data_ok: bool) -> dict:
    """无锚点时的建立引导（仅教练附注区，ADR-0005）：给出可作锚点来源的候选场次。

    候选限定「早于当前窗口起点」——锚点是窗口前的参照系，用窗口内场次会让差值恒为 0；
    与滚动基线同一时间语义，保证建立后的方向判定有意义。按时间升序（赛季初场次在前）。

    仅当「建立锚点确实能让本窗口出方向判定」时才引导：降级期 / 样本不足 / 数据存疑时，
    即便建立锚点也不会解锁判定，故抑制引导并说明真实原因（避免给出无效动作）。

    候选还需满足记分箭最小样本（`data_quality.min_shots_for_description`）：过小的场次作锚点
    噪声底过高，会使后续方向判定长期不可信；门槛与 `create_anchor_snapshot` 的校验对齐。
    """
    if primary_bow is None:
        return {"needs_anchor": False, "primary_bow": None, "candidates": [],
                "reason": "本窗口无箭，无法确定弓种"}
    if anchor_dict:
        return {"needs_anchor": False, "primary_bow": primary_bow, "candidates": [],
                "reason": "该弓种已有锚点"}
    if get_config().mdc_source is None:
        return {"needs_anchor": False, "primary_bow": primary_bow, "candidates": [],
                "reason": "降级期（MDC 阈值口径试行中），建立锚点暂不会解锁方向判定"}
    if not data_ok:
        return {"needs_anchor": False, "primary_bow": primary_bow, "candidates": [],
                "reason": "本窗口数据存疑（分布存在结构性矛盾），先核查数据再建立锚点"}
    if not gate_ok:
        return {"needs_anchor": False, "primary_bow": primary_bow, "candidates": [],
                "reason": "本窗口样本不足，建立锚点后仍需样本达标才出方向判定"}
    rows = db.anchor_candidate_sessions(athlete_id, primary_bow)
    before_window = [
        r for r in rows
        if window_start is None or (r["session_time_utc"] or "") < window_start
    ]
    min_shots = get_config().data_quality.min_shots_for_description
    candidates = [
        {"session_id": r["session_id"], "session_time_utc": r["session_time_utc"],
         "distance_m": r["distance_m"], "mode_composition": r["mode_composition"],
         "scoring_shots": r["scoring_shots"]}
        for r in before_window
        if (r["scoring_shots"] or 0) >= min_shots
    ][:10]
    if candidates:
        reason = "当前无锚点：锚点是方向判定的参照系，建议选赛季初/入队测试场次建立"
    elif before_window:
        reason = (f"当前无锚点，且早于本窗口的场次记分箭均不足 {min_shots} 支，"
                  "样本过小不足以作锚点参照")
    else:
        reason = "当前无锚点，且无早于本窗口的候选场次可作参照"
    return {"needs_anchor": True, "primary_bow": primary_bow,
            "candidates": candidates, "reason": reason}


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
