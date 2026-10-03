# -*- coding: utf-8 -*-
"""v1.2 消息校验：对接包 schema.json（draft 2020-12）+ 文档硬约束补充校验。

schema.json 是开发侧对接包《射箭电子靶数据接口-对接包.schema.json》的逐字节副本
（sha256 见 SCHEMA_SHA256；文档声明"字段冲突以 schema 为准"）。schema 没有机器化、
但主文档明文规定的硬约束在这里补齐（只收紧、不放宽）：

1. 时间一律 UTC 且以 Z 结尾（文档首页"时区"）——schema 的 format: date-time 会放行 +08:00
2. 实时流 dt1/dt4 的 shotId 与 shotSeq 同步可空（§3.9：不出现一空一有的混合态）
3. subType 仅 dataType=9 使用（schema 字段描述）

返回值：错误字符串列表（空 = 合法）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).with_name("schema.json")
SCHEMA_SHA256 = "eaa305d2bfdbcf3172a3422da013304c9c332b0c7fa806fe2db3efdcaac7882a"
MAX_ERRORS = 10

# 文档中所有时间字段（信封 sentAt + 各 dt 的业务时间）
_TIME_FIELDS = ("timestamp", "releaseTime", "hitTime", "sessionEndTime", "createdAt",
                "snapshotTime", "windowStart", "windowEnd")
_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")


@lru_cache(maxsize=1)
def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def parse_utc_z(value: str) -> datetime | None:
    """严格解析 UTC Z 时间；不合规返回 None。"""
    if not isinstance(value, str) or not _UTC_Z.match(value):
        return None
    head, _, frac = value[:-1].partition(".")
    frac = (frac + "000000")[:6] if frac else "000000"
    try:
        return datetime.fromisoformat(f"{head}.{frac}+00:00")
    except ValueError:
        return None


def _path_of(err) -> str:
    parts = [str(p) for p in err.absolute_path]
    return ".".join(parts) if parts else "(root)"


def validate_message(msg: Any) -> list[str]:
    if not isinstance(msg, dict):
        return ["消息必须是 JSON 对象"]
    errors: list[str] = []
    for err in sorted(_validator().iter_errors(msg), key=lambda e: list(map(str, e.absolute_path))):
        errors.append(f"{_path_of(err)}: {err.message}")
        if len(errors) >= MAX_ERRORS:
            break
    if errors:
        return errors

    # ---- 文档硬约束补充 ----
    if "sentAt" in msg and parse_utc_z(msg["sentAt"]) is None:
        errors.append(f"sentAt: 时间必须为 UTC 且以 Z 结尾（文档首页时区约定），收到 {msg['sentAt']!r}")
    data = msg["data"]
    for f in _TIME_FIELDS:
        if f in data and parse_utc_z(data[f]) is None:
            errors.append(f"data.{f}: 时间必须为 UTC 且以 Z 结尾（文档首页时区约定），收到 {data[f]!r}")
    dt = msg["dataType"]
    if dt in (1, 4) and (data.get("shotId") is None) != (data.get("shotSeq") is None):
        errors.append("data.shotId/shotSeq: 实时流两者须同步可空（§3.9，不允许一空一有的混合态）")
    if dt != 9 and "subType" in msg:
        errors.append("subType: 仅 dataType=9 使用")
    return errors
