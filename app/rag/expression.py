"""Project a visible saved report to the limited input used for model expression."""
from __future__ import annotations

import hashlib
import json
import re
from unicodedata import normalize

from app.privacy import CONTACT, MEDIA_PATH
from app.rag.context import extract_numbers
from app.reports.window import WindowKey

_METRICS = {
    "avg_score": ("avgScore", "均环"),
    "mcr_t": ("mcrT", "撒放用时"),
    "dispersion_mm": ("dispersionMm", "散布"),
    "hr_volatility": ("hrVolatility", "心率波动"),
}
_METRIC_IDS = {metric for metric, _ in _METRICS.values()}
_RULE_REF = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_VERDICT_FIELDS = ("verdict", "reason", "tier", "value", "anchor", "delta", "mdc")


def expression_input(report: dict, notes: list[dict], history, profile_name: str | None, view: str,
                     *, baseline: dict, question: str) -> dict:
    key = WindowKey.parse(report["granularity"], report["window_key"])
    identifiers = (profile_name, report["athlete"]["id"], key.session_id)

    def safe_text(text: str) -> str:
        value = normalize("NFKC", text)
        value = MEDIA_PATH.sub("〔媒体已隐藏〕", CONTACT.sub("〔身份信息已隐藏〕", value))
        for identifier in identifiers:
            if identifier:
                value = value.replace(normalize("NFKC", identifier), "〔身份信息已隐藏〕")
        return value

    extra = report.get("coach_extra", {}) if view == "coach" else {}
    evidence = extra.get("evidence", {})
    sections = []
    for section in report["sections"]:
        items = section.get("evidence") or evidence.get(section["key"], [])
        sections.append({
            "key": section["key"],
            "title": safe_text(section["title"]),
            "content": [safe_text(str(line)) for line in section["content"]],
            "evidence": [
                {"type": item["type"], "ref": item["ref"], "value": _numeric_evidence(item.get("value"))}
                for item in items if item.get("type") in ("fact", "calc", "judge")
                and _RULE_REF.fullmatch(item.get("ref", ""))
            ] if view == "coach" else [],
        })

    metrics = {k: v for k, v in report.get("metrics", {}).items() if type(v) in (int, float)}
    verdicts = {}
    for metric, verdict in report.get("conclusions", {}).get("verdicts", {}).items():
        if metric in _METRIC_IDS:
            verdicts[metric] = {k: safe_text(v) if isinstance(v, str) else v
                                for k in _VERDICT_FIELDS if (v := verdict.get(k)) is not None}
    window_dates = {k: day for k in ("window_start", "window_end")
                    if _ISO_DAY.fullmatch(day := (report.get(k) or "")[:10])}
    facts = {"sections": sections, "metrics": metrics, "verdicts": verdicts,
             "rolling_baseline": baseline, **window_dates}

    guide = report["rule_guidance"]
    pack = report.get("rule_pack")
    references = [_reference("window", f"{report['granularity']}:{report['window_key']}", "本期报告窗口")]
    references.extend(_reference("metric", metric, title) for field, (metric, title) in _METRICS.items()
                      if field in metrics)
    if baseline:
        references.append(_reference("baseline", "rolling", "滚动基线"))
    rules = [_reference("rule", b["source"], "规则建议") for b in guide["bullets"]]
    references.extend(rules)
    # The prose of notes and templates can contain arbitrary personal information.
    # Pass visible note categories and traceable references, without exporting the prose.
    visible_notes = [_reference("note", str(note["id"]), f"备注·{note['note_type']}") for note in notes]
    references.extend(visible_notes)
    memory = _reference("memory", str(history["id"]), "历史结论") if history else None
    if memory:
        references.append(memory)
    payload = {
        "granularity": report["granularity"],
        "window_key": safe_text(key.text) if key.session_id is None else None,
        **facts,
        "notes": visible_notes,
        "history": memory,
        "rules": rules,
        "rule_pack_version": safe_text(pack["version"]) if pack else None,
    }
    skeleton = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    number_values = {
        **facts,
        "sections": [section["content"] for section in sections],
        "evidence": [item["value"] for section in sections for item in section["evidence"]],
    }
    return {
        "skeleton_json": skeleton,
        "version": hashlib.sha256(skeleton.encode("utf-8")).hexdigest()[:12],
        "numbers": extract_numbers("\n".join(_values(number_values))),
        "judgement_lines": [line for section in sections for line in section["content"]],
        "model_question": safe_text(question),
        "report_id": report["report_id"],
        "rule_guidance": guide,
        "rule_pack": pack,
        "references": list({item["ref"]: item for item in references}.values()),
    }


def _reference(kind: str, key: str, title: str) -> dict:
    return {"type": kind, "ref": f"{kind}:{key}", "title": title}


def _numeric_evidence(value):
    if isinstance(value, dict):
        return {k: _numeric_evidence(v) for k, v in value.items()
                if k.isascii() and re.fullmatch(r"[a-z0-9_]+", k) and k != "id" and not k.endswith("_id")}
    if isinstance(value, list):
        return [_numeric_evidence(item) for item in value]
    return value if type(value) in (int, float, bool) else None


def _values(data):
    if isinstance(data, dict):
        data = data.values()
    elif not isinstance(data, (list, tuple)):
        if type(data) in (str, int, float):
            yield str(data)
        return
    for value in data:
        yield from _values(value)
