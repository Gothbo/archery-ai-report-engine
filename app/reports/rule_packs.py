"""Versioned report parameters and their publication and signing history."""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Annotated, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import EngineConfig, MDCSpec, REQUIRED_MDC_KEYS, SampleThreshold
from app.reports.advice import DEFAULT_TEMPLATES
from app.timeutil import iso_now_utc

if TYPE_CHECKING:
    from sqlite3 import Connection, Row

    from app.store.database import Database

DEFAULT_VERSION = "v1-trial-20261010"
VERSION_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$"
_DIRECTIONS = {"avgScore": 1, "mcrT": -1, "hrVolatility": -1, "dispersionMm": -1}
_Nonnegative = Annotated[float, Field(ge=0, strict=True, allow_inf_nan=False)]
_PositiveInt = Annotated[int, Field(ge=1, strict=True)]
_Text = Annotated[str, Field(min_length=1, max_length=500)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ChangeBand(_Input):
    max: _Nonnegative
    te: _Nonnegative | None = None
    swc: _Nonnegative | None = None


class MetricThresholds(_Input):
    threshold: _Nonnegative | None = None
    te: _Nonnegative | None = None
    swc: _Nonnegative | None = None
    bands: list[ChangeBand] = Field(default_factory=list)

    @field_validator("bands")
    @classmethod
    def ascending_bands(cls, bands: list[ChangeBand]) -> list[ChangeBand]:
        if any(a.max >= b.max for a, b in zip(bands, bands[1:])):
            raise ValueError("指标分档上限必须严格升序")
        return bands


class PackSampleThreshold(SampleThreshold):
    model_config = ConfigDict(extra="forbid", strict=True)


class PackRules(_Input):
    mdc: dict[str, MetricThresholds]
    wind_bands: list[tuple[_Nonnegative, _Nonnegative]] = Field(min_length=1)
    min_wind_band_shots: _PositiveInt
    wind_gap_split_mps: Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)]
    min_wind_gap_shots: _PositiveInt
    sample_threshold: PackSampleThreshold
    advice_templates: dict[str, _Text]

    @field_validator("mdc")
    @classmethod
    def known_metrics(cls, thresholds: dict[str, MetricThresholds]) -> dict[str, MetricThresholds]:
        if thresholds.keys() != set(REQUIRED_MDC_KEYS):
            raise ValueError("MDC 指标必须为 " + "、".join(REQUIRED_MDC_KEYS))
        return thresholds

    @field_validator("wind_bands")
    @classmethod
    def contiguous_wind_bands(cls, bands: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if any(lo >= hi for lo, hi in bands):
            raise ValueError("每个风档的下限必须小于上限")
        if any(a[1] != b[0] for a, b in zip(bands, bands[1:])):
            raise ValueError("风档必须连续无重叠")
        return bands

    @field_validator("advice_templates")
    @classmethod
    def safe_templates(cls, templates: dict[str, str]) -> dict[str, str]:
        if templates.keys() != DEFAULT_TEMPLATES.keys():
            raise ValueError("建议模板键必须与默认规则包相同")
        formatter = Formatter()
        for key, text in templates.items():
            if any(mark in text for mark in ("专家共识", "已签署")):
                raise ValueError(f"模板 {key} 不得声明规则包的签署状态")
            allowed = {field for _, field, _, _ in formatter.parse(DEFAULT_TEMPLATES[key]) if field is not None}
            for _, field, spec, conversion in formatter.parse(text):
                if field is not None and (field not in allowed or spec or conversion):
                    raise ValueError(f"模板 {key} 只允许使用已有的简单占位符")
        return templates

    def apply(self, config: EngineConfig, version: str, status: str) -> EngineConfig:
        thresholds = {key: MDCSpec(direction=_DIRECTIONS[key], **value.model_dump())
                      for key, value in self.mdc.items()}
        effective = config.model_copy(update={
            "mdc": thresholds,
            "mdc_source": version,
            "wind_bands": [list(band) for band in self.wind_bands],
            "sample_threshold": self.sample_threshold,
            "advice": config.advice.model_copy(update={"min_band_shots": self.min_wind_band_shots}),
            "conclusions": config.conclusions.model_copy(update={
                "wind_gap_split_mps": self.wind_gap_split_mps,
                "wind_gap_min_shots": self.min_wind_gap_shots,
                "provisional": status == "trial",
            }),
        })
        effective._rule_pack_version = version
        return effective


class PackSource(_Input):
    title: _Text
    reference: _Text


class PackActor(_Input):
    actor_id: Annotated[str, Field(min_length=1, max_length=128)]


class PackDraft(PackActor):
    rules: PackRules
    sources: list[PackSource] = Field(min_length=1, max_length=30)
    review_date: date


class PackNotFound(ValueError):
    pass


class PackConflict(ValueError):
    pass


@lru_cache(maxsize=1)
def _seed_data() -> tuple[str, str, str, str, str]:
    path = Path(__file__).with_name("default_rule_pack.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    effective_at = data.pop("effective_at_utc")
    data["rules"]["advice_templates"] = DEFAULT_TEMPLATES
    seed = PackDraft.model_validate(data)
    sources = json.dumps([source.model_dump() for source in seed.sources], ensure_ascii=False)
    return seed.rules.model_dump_json(), sources, seed.review_date.isoformat(), seed.actor_id, effective_at


def seed_rule_pack(conn: Connection) -> None:
    rules, sources, review_date, actor_id, effective_at = _seed_data()
    conn.execute(
        """INSERT OR IGNORE INTO rule_packs
           (version, status, rules_json, sources_json, review_date, updated_by, updated_at_utc, effective_at_utc)
           VALUES (?, 'trial', ?, ?, ?, ?, ?, ?)""",
        (DEFAULT_VERSION, rules, sources, review_date, actor_id, effective_at, effective_at),
    )


def _unpack(row: Row) -> dict:
    result = dict(row)
    result["rules"] = json.loads(result.pop("rules_json"))
    result["sources"] = json.loads(result.pop("sources_json"))
    return result


def active_pack(db: Database, config: EngineConfig) -> dict | None:
    rows = db.query(
        """SELECT p.* FROM rule_packs p JOIN rule_pack_audit a ON a.version=p.version
           WHERE a.action='publish' ORDER BY a.id DESC LIMIT 1""")
    if rows:
        return _unpack(rows[0])
    # A deliberately disabled or alternative legacy configuration is preserved
    # until an administrator publishes a rule pack in this database.
    return get_pack(db, DEFAULT_VERSION) if config.mdc_source == DEFAULT_VERSION else None


def get_pack(db: Database, version: str) -> dict:
    rows = db.query("SELECT * FROM rule_packs WHERE version=?", (version,))
    if not rows:
        raise PackNotFound("规则包版本不存在")
    return _unpack(rows[0])


def list_packs(db: Database) -> list[dict]:
    return [_unpack(row) for row in db.query("SELECT * FROM rule_packs ORDER BY updated_at_utc DESC, version")]


def save_draft(db: Database, version: str, draft: PackDraft) -> dict:
    with db.lock, db.conn:
        result = db.conn.execute(
            """INSERT INTO rule_packs (version, status, rules_json, sources_json, review_date,
                                       updated_by, updated_at_utc)
               VALUES (?, 'draft', ?, ?, ?, ?, ?)
               ON CONFLICT(version) DO UPDATE SET rules_json=excluded.rules_json,
                 sources_json=excluded.sources_json, review_date=excluded.review_date,
                 updated_by=excluded.updated_by, updated_at_utc=excluded.updated_at_utc
               WHERE rule_packs.status='draft'""",
            (version, draft.rules.model_dump_json(),
             json.dumps([source.model_dump() for source in draft.sources], ensure_ascii=False),
             draft.review_date.isoformat(), draft.actor_id, iso_now_utc()),
        )
        if not result.rowcount:
            raise PackConflict("只有草稿可以修改，已发布规则请创建新版本")
        return get_pack(db, version)


def publish_pack(db: Database, version: str, actor_id: str) -> dict:
    return _transition(db, version, actor_id, "publish")


def sign_pack(db: Database, version: str, actor_id: str) -> dict:
    return _transition(db, version, actor_id, "sign")


def _transition(db: Database, version: str, actor_id: str, action: str) -> dict:
    now = iso_now_utc()
    with db.lock, db.conn:
        if action == "publish":
            result = db.conn.execute(
                """UPDATE rule_packs SET status='trial', effective_at_utc=?, updated_at_utc=?, updated_by=?
                   WHERE version=? AND status='draft'""", (now, now, actor_id, version))
        else:
            result = db.conn.execute(
                """UPDATE rule_packs SET status='signed', signed_at_utc=?, signed_by=?
                   WHERE version=? AND status='trial'""", (now, actor_id, version))
        pack = get_pack(db, version)
        if not result.rowcount:
            message = "只有草稿可以发布" if action == "publish" else "只有试行规则包可以签署"
            raise PackConflict(message)
        if action == "publish":
            PackRules.model_validate(pack["rules"])
        db.conn.execute(
            "INSERT INTO rule_pack_audit(version, action, actor_id, occurred_at_utc) VALUES (?, ?, ?, ?)",
            (version, action, actor_id, now))
        return pack


def audit_events(db: Database, version: str) -> list[dict]:
    get_pack(db, version)
    return [dict(row) for row in db.query(
        "SELECT version, action, actor_id, occurred_at_utc FROM rule_pack_audit WHERE version=? ORDER BY id",
        (version,))]


def pack_metadata(pack: dict) -> dict:
    return {key: pack[key] for key in ("version", "status", "sources", "review_date", "effective_at_utc",
                                      "updated_by", "signed_by", "signed_at_utc")}
