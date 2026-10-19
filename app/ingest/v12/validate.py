# -*- coding: utf-8 -*-
"""v1.2 消息校验：按 schemaVersion 路由到对应 schema（draft 2020-12）+ 文档硬约束补充校验。

支持的协议版本（接口总文档 v1.2 修订版 §4.4"版本规则"）：
- "1.1"：开发侧原对接包《射箭电子靶数据接口-对接包.schema.json》v1.1 的逐字节副本 schema.json，
  兼容尚未升级的发送端（该版本下脱靶 score=0、releaseTime 缺失仍判为非法）。
- "1.2"：PM 侧修订版对接包 schema（schema_v1_2.json，逐字节副本）：脱靶 miss 显式标记、
  releaseTime 可选、UTC 尾 Z / shotId·shotSeq 同步可空 / subType 限定 已写进 schema。
- 其他值：拒收，并在错误里说明支持的版本。

schema 没有机器化、但主文档明文规定的硬约束在这里对所有版本统一补齐（只收紧、不放宽；
对 1.2 来说与 schema 重复，起兜底作用）：
1. 时间一律 UTC 且以 Z 结尾（文档首页"时区"）
2. 实时流 dt1/dt4 的 shotId 与 shotSeq 同步可空（§3.9）
3. subType 仅 dataType=9 使用

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

_HERE = Path(__file__).parent
# 协议版本 → (vendored schema 路径, sha256)。sha256 由测试核对，保证与交付文件逐字节一致。
SCHEMAS: dict[str, tuple[Path, str]] = {
    "1.1": (_HERE / "schema.json", "eaa305d2bfdbcf3172a3422da013304c9c332b0c7fa806fe2db3efdcaac7882a"),
    "1.2": (_HERE / "schema_v1_2.json", "118db8281ede9632f2322b3a96bd30bed277323364e02c762504ee026128759c"),
}
SUPPORTED_VERSIONS = tuple(SCHEMAS)
LATEST_VERSION = "1.2"
# 兼容旧引用（v1.1 schema）
SCHEMA_PATH, SCHEMA_SHA256 = SCHEMAS["1.1"]
MAX_ERRORS = 10

# 文档中所有时间字段（信封 sentAt + 各 dt 的业务时间）
_TIME_FIELDS = ("timestamp", "releaseTime", "hitTime", "sessionEndTime", "createdAt",
                "snapshotTime", "windowStart", "windowEnd")
_UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z$")


@lru_cache(maxsize=None)
def load_schema(version: str = "1.1") -> dict:
    return json.loads(SCHEMAS[version][0].read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _validator(version: str) -> Draft202012Validator:
    schema = load_schema(version)
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
    version = msg.get("schemaVersion")
    if version not in SCHEMAS:
        return [f"schemaVersion: 不支持的协议版本 {version!r}（接收端支持 {', '.join(SUPPORTED_VERSIONS)}）"]
    errors: list[str] = []
    for err in sorted(_validator(version).iter_errors(msg), key=lambda e: list(map(str, e.absolute_path))):
        errors.append(f"{_path_of(err)}: {err.message}")
        if len(errors) >= MAX_ERRORS:
            break
    if errors:
        return errors

    # ---- 文档硬约束补充（所有版本）----
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
