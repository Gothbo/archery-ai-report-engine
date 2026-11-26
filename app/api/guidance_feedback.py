"""Guidance feedback submission and administrator review."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.api.rule_packs import require_admin
from app.store.database import get_database
from app.store.guidance_feedback import FeedbackReportNotFound, feedback_summary, submit_feedback
from app.timeutil import format_utc_ms

router = APIRouter(prefix="/api/v1", tags=["guidance"])

_Key = Annotated[str, Field(min_length=1, max_length=128)]
_DEFAULT_DAYS = 90
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    report_id: _Key
    granularity: Literal["daily", "weekly", "monthly", "quarterly", "yearly"]
    window_key: _Key
    view: Literal["athlete", "coach"]
    rule_pack_version: str = Field(default="", max_length=64)
    source: Literal["rule", "model"]
    rating: int = Field(strict=True, ge=1, le=5)
    will_follow: bool = Field(strict=True)
    reason: str | None = Field(default=None, max_length=120)
    rules: list[_Key] = Field(default_factory=list, max_length=64)
    actor_id: _Key
    submitted_at_utc: AwareDatetime

    @field_validator("submitted_at_utc", mode="before")
    @classmethod
    def require_iso_string(cls, value):
        if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
            raise ValueError("提交时间须为带时区的 ISO 8601 字符串")
        return value


@router.post("/athletes/{athlete_id}/guidance/feedback")
def save_feedback(athlete_id: str, body: FeedbackRequest) -> dict:
    values = body.model_dump()
    try:
        values["submitted_at_utc"] = format_utc_ms(body.submitted_at_utc)
        feedback_id = submit_feedback(get_database(), athlete_id, values)
    except FeedbackReportNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": feedback_id, "accepted": True}


@router.get("/guidance/feedback/summary", dependencies=[Depends(require_admin)])
def read_feedback_summary(start: Annotated[date | None, Query(alias="from")] = None,
                          end: Annotated[date | None, Query(alias="to")] = None) -> dict:
    end = end or datetime.now(timezone.utc).date()
    start = start or date.fromordinal(max(1, end.toordinal() - _DEFAULT_DAYS + 1))
    if start > end:
        raise HTTPException(422, "from 日期不得晚于 to")
    return feedback_summary(get_database(), start, end)
