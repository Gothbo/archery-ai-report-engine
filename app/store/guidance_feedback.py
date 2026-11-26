"""Store feedback separately from reports and summarize their observed rule hits."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date
from statistics import mean
from typing import TYPE_CHECKING
from unicodedata import normalize
from uuid import uuid4

from app.privacy import CONTACT, MEDIA_PATH
from app.reports.rule_guidance import guidance_for_report, rule_sources
from app.reports.window import WindowKey

if TYPE_CHECKING:
    from sqlite3 import Connection, Row

    from app.store.database import Database

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_NAME_LABEL = re.compile(r"(?:姓名|身份证(?:号)?|手机号)\s*[:：]")
_LOW_RATING = 2


class FeedbackReportNotFound(ValueError):
    pass


def _report_body(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _pack_version(body: dict, stored_version: str | None) -> str:
    return stored_version or (body.get("rule_pack") or {}).get("version") or ""


def record_report_observation(conn: Connection, report_id: str, generated_at_utc: str,
                              rule_pack_version: str | None, report_json: str | None) -> None:
    body = _report_body(report_json)
    if body is not None:
        conn.execute(
            """INSERT OR IGNORE INTO guidance_report_observations
               (report_id, generated_at_utc, rule_pack_version, rules_json) VALUES (?, ?, ?, ?)""",
            (report_id, generated_at_utc, _pack_version(body, rule_pack_version),
             json.dumps(rule_sources(guidance_for_report(body)))),
        )


def backfill_report_observations(conn: Connection) -> None:
    for row in conn.execute(
        """SELECT r.report_id, r.generated_at_utc, r.rule_pack_version, r.report_json
           FROM report_cache r WHERE r.report_json IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM guidance_report_observations o WHERE o.report_id=r.report_id)"""
    ):
        record_report_observation(conn, *row)


def _window(granularity: str, value: str) -> str:
    if granularity == "daily" and _DAY.fullmatch(value):
        return WindowKey.calendar_day(date.fromisoformat(value)).text
    key = WindowKey.parse(granularity, value)
    if key.is_calendar_day:
        return WindowKey.calendar_day(date.fromisoformat(key.value[4:])).text
    return key.text


def _validate_reason(reason: str | None, athlete_name: str | None) -> None:
    if not reason:
        return
    text = normalize("NFKC", reason)
    if len(text.splitlines()) > 1:
        raise ValueError("评分原因只能写一行")
    if CONTACT.search(text) or MEDIA_PATH.search(text) or _NAME_LABEL.search(text):
        raise ValueError("评分原因不能包含个人身份信息或视频路径")
    if athlete_name and len(athlete_name) > 1 and normalize("NFKC", athlete_name) in text:
        raise ValueError("评分原因不能包含运动员姓名")


def submit_feedback(db: Database, athlete_id: str, values: dict) -> str:
    with db.lock, db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        report = db.get_report_row(values["report_id"])
        if report is None or report["athlete_id"] != athlete_id or report["granularity"] != values["granularity"]:
            raise FeedbackReportNotFound("报告不存在或与运动员和窗口不匹配")
        body = _report_body(report["report_json"])
        if body is None or _window(values["granularity"], values["window_key"]) != report["window_key"]:
            raise FeedbackReportNotFound("报告不存在或与运动员和窗口不匹配")

        version = _pack_version(body, report["rule_pack_version"])
        if values["rule_pack_version"] != version:
            raise ValueError("评分的规则包版本与已保存报告不一致")
        matched = rule_sources(guidance_for_report(body))
        rules = list(dict.fromkeys(values["rules"]))
        if set(rules) - set(matched):
            raise ValueError("评分包含报告未命中的规则")
        profile = db.get_profile(athlete_id)
        reason = values["reason"] or None
        _validate_reason(reason, profile["name"] if profile else None)

        record_report_observation(db.conn, report["report_id"], report["generated_at_utc"],
                                  version, report["report_json"])
        bound = {**values, "id": uuid4().hex, "athlete_id": athlete_id, "window_key": report["window_key"],
                 "rule_pack_version": version, "reason": reason, "rules_json": json.dumps(rules)}
        db.conn.execute(
            """INSERT INTO guidance_feedback
               (id, report_id, athlete_id, granularity, window_key, view, rule_pack_version, source,
                rating, will_follow, reason, rules_json, actor_id, submitted_at_utc)
               VALUES (:id, :report_id, :athlete_id, :granularity, :window_key, :view, :rule_pack_version, :source,
                       :rating, :will_follow, :reason, :rules_json, :actor_id, :submitted_at_utc)
               ON CONFLICT (report_id, actor_id) DO UPDATE SET
                   view=excluded.view, source=excluded.source, rating=excluded.rating,
                   will_follow=excluded.will_follow, reason=excluded.reason, rules_json=excluded.rules_json,
                   submitted_at_utc=excluded.submitted_at_utc""", bound,
        )
        return db.conn.execute(
            "SELECT id FROM guidance_feedback WHERE report_id=? AND actor_id=?",
            (values["report_id"], values["actor_id"]),
        ).fetchone()["id"]


def feedback_summary(db: Database, start: date, end: date) -> dict:
    bounds = (f"{start.isoformat()}T00:00:00.000Z", f"{end.isoformat()}T23:59:59.999Z")
    with db.lock, db.conn:
        db.conn.execute("BEGIN")
        observations = db.query(
            """SELECT report_id, rules_json FROM guidance_report_observations
               WHERE generated_at_utc BETWEEN ? AND ?""", bounds)
        feedback = db.query(
            """SELECT * FROM guidance_feedback WHERE submitted_at_utc BETWEEN ? AND ?
               ORDER BY submitted_at_utc DESC, rowid DESC""", bounds)

    observed = {row["report_id"] for row in observations}
    hits = Counter(rule for row in observations for rule in json.loads(row["rules_json"]))
    rated: dict[str, list[Row]] = defaultdict(list)
    by_version: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for item in feedback:
        if item["report_id"] in observed:
            for rule in json.loads(item["rules_json"]):
                rated[rule].append(item)
                by_version[rule, item["rule_pack_version"]].append(item)

    review = []
    streaks: Counter[str] = Counter()
    for (rule, version), samples in sorted(by_version.items()):
        low = _consecutive_low_reports(samples)
        streaks[rule] = max(streaks[rule], len(low))
        if len(low) >= 2:
            latest = low[:2]
            review.append({
                "rule": rule, "rule_pack_version": version, "low_rounds": len(low),
                "avg_rating": _average(latest, "rating"),
                "samples": [_review_sample(item) for item in latest],
            })

    return {
        "total": len(feedback), "avg_rating": _average(feedback, "rating"),
        "will_follow_rate": _average(feedback, "will_follow"),
        "rules": [
            {"rule": rule, "hits": hits[rule], "rated": len(rated[rule]),
             "avg_rating": _average(rated[rule], "rating"), "will_follow_rate": _average(rated[rule], "will_follow"),
             "low_streak": streaks[rule], "in_review": streaks[rule] >= 2}
            for rule in sorted(hits)
        ],
        "low_reason_distribution": dict(Counter(item["reason"] or "" for item in feedback
                                                 if item["rating"] <= _LOW_RATING)),
        "review_queue": review,
    }


def _consecutive_low_reports(samples: list[Row]) -> list[Row]:
    distinct: dict[str, Row] = {}
    for item in samples:
        if item["rating"] > _LOW_RATING:
            break
        distinct.setdefault(item["report_id"], item)
    return list(distinct.values())


def _average(rows: list[Row], key: str) -> float | None:
    return mean(row[key] for row in rows) if rows else None


def _review_sample(item: Row) -> dict:
    return {
        "report_id": item["report_id"], "rating": item["rating"], "will_follow": bool(item["will_follow"]),
        "reason": item["reason"], "submitted_at_utc": item["submitted_at_utc"],
    }
