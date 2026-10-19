# -*- coding: utf-8 -*-
"""运动员身份脱敏（P0 隐私）：外部身份标识 → 不可逆的内部 athlete_id。

- 算法：HMAC-SHA256(密钥, "athlete:" + 规范化标识)，取前 64 bit 映射为 19 位纯数字字符串
  （保持旧版"雪花格式 19 位数字字符串"的外形，下游/前端无需改动）。
- 不可逆：没有密钥无法从 athlete_id 还原身份证号；有密钥也只能"正向比对"，不能反推。
- 输入可以是身份证号（display_sys 真库）或 v1.2 接口的 athleteId（平台业务 ID），
  两条路径用同一函数，同一标识得到同一 athlete_id。
- 密钥来源（优先级）：环境变量 ENGINE_ID_SECRET → config.store.id_secret_path →
  与 facts.db 同目录的 athlete_id.key（首次使用自动生成 32 字节随机密钥，权限 600）。
  **密钥丢失 = 全部 athlete_id 改变（历史数据与新导入数据无法关联），须随 facts.db 一起备份。**
- 旧版 athlete_id（10^18 + 身份证号，可逆）的迁移见 scripts/migrate_athlete_ids.py。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

from app.config import get_config

logger = logging.getLogger("engine.identity")

ENV_SECRET = "ENGINE_ID_SECRET"
DEFAULT_KEY_FILENAME = "athlete_id.key"

_KEY_CACHE: dict[str, bytes] = {}


def _key_path() -> Path:
    store = get_config().store
    if store.id_secret_path:
        return Path(store.id_secret_path)
    return Path(store.db_path).resolve().parent / DEFAULT_KEY_FILENAME


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        key = path.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError(f"身份脱敏密钥文件为空：{path}")
        return key.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    logger.warning("已生成身份脱敏密钥 %s（请与 facts.db 一起备份；丢失后 athlete_id 全部改变）", path)
    return key.encode("utf-8")


def id_secret() -> bytes:
    env = os.environ.get(ENV_SECRET, "").strip()
    if env:
        return env.encode("utf-8")
    path = _key_path()
    cache_key = str(path)
    if cache_key not in _KEY_CACHE:
        _KEY_CACHE[cache_key] = _load_or_create_key(path)
    return _KEY_CACHE[cache_key]


def normalize_identity(identity) -> str:
    return str(identity).strip().upper()


def pseudonymize_athlete(identity, secret: bytes | None = None) -> str:
    """外部身份标识 → 19 位数字字符串 athlete_id（确定性、不可逆、大小写无关）。"""
    key = secret if secret is not None else id_secret()
    digest = hmac.new(key, b"athlete:" + normalize_identity(identity).encode("utf-8"), hashlib.sha256).digest()
    return str(10**18 + int.from_bytes(digest[:8], "big") % (9 * 10**18))


def legacy_athlete_id(identity) -> str:
    """旧版（≤ v0.1）映射，仅供迁移脚本比对旧库使用：纯数字 18 位 = 10^18+号码（可逆！），含 X 走 SHA1。"""
    import re

    ident = normalize_identity(identity)
    if re.fullmatch(r"\d{18}", ident):
        return str(10**18 + int(ident))
    h = hashlib.sha1(ident.encode("utf-8")).hexdigest()
    return str(10**18 + int(h[:14], 16) % 10**18)
