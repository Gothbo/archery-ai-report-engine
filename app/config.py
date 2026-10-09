# -*- coding: utf-8 -*-
"""口径配置加载（config.json → pydantic 模型，严格校验，缺键/方向错误启动失败）。

口径来源：
- 工程化方案 D7（timezone/sample_threshold/wind_bands）、D4.1（mdc/mdc_source）、D4（forbidden_phrases）
- 记忆系统方案 §3.1（rolling_window_shots/notes_visibility）
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("ENGINE_CONFIG", BASE_DIR / "config.json"))

# D4 SSOT：规则引擎引用的指标键必须在此声明（缺键 = 启动失败，防止改名漏改方向类问题）
REQUIRED_MDC_KEYS = ("avgScore", "mcrT", "hrVolatility", "dispersionMm")


class MDCSpec(BaseModel):
    threshold: float | None = None
    direction: int = Field(ge=-1, le=1)


class SampleThreshold(BaseModel):
    daily: int = Field(gt=0)
    weekly: int = Field(gt=0)
    monthly: int = Field(gt=0)
    quarterly: int = Field(gt=0)
    yearly: int = Field(gt=0)


class NotesVisibility(BaseModel):
    athlete_visible_types: list[str] = Field(min_length=1)


class AnchorRebuildPolicy(BaseModel):
    triggers: list[str] = Field(min_length=1)
    check_timing: str = "weekly_report_generation"
    four_week_min_weeks: int = Field(default=4, ge=1)


class MemoryConfig(BaseModel):
    rolling_snapshot_keep: int = Field(default=12, gt=0)
    anchor_rebuild_policy: AnchorRebuildPolicy
    notes_display_days: int = Field(default=14, gt=0)


class StoreConfig(BaseModel):
    db_path: str = "facts.db"
    sqlite_source_path: str | None = None
    sqlite_min_time: str = "2024-01-01"  # M5：跳过早期测试数据（2009 等）
    session_gap_minutes: int = Field(default=90, gt=0)  # M5：同日间隔 >N 分钟拆场
    default_distance_m: int = Field(default=70, gt=0)   # M5：真库无距离字段，默认反曲弓 70m
    shot_type_map: dict[str, int] = Field(default_factory=lambda: {"1": 0, "3": 1, "7": 1})
    project_bow_map: dict[str, str] = Field(default_factory=lambda: {"171": "反曲弓"})


class LLMConfig(BaseModel):
    """对话增强（B1）：默认关闭；enabled=false 时其余字段可留空（取默认值）。"""

    enabled: bool = False
    provider: str = "llamacpp"  # llamacpp（OpenAI 兼容）| ollama | mock（演示/测试）
    endpoint: str = "http://127.0.0.1:8080"
    model: str = "qwen2.5-1.5b-instruct-q4_k_m"
    model_version: str = "qwen2.5-1.5b-v1"  # 缓存/口径版本键（换模型不串上下文）
    timeout_sec: int = Field(default=180, gt=0)
    max_retries: int = Field(default=1, ge=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    num_ctx: int = Field(default=2048, gt=0)

    @field_validator("provider")
    @classmethod
    def _provider_enum(cls, v: str) -> str:
        if v not in ("llamacpp", "ollama", "mock"):
            raise ValueError("provider 必须为 llamacpp | ollama | mock")
        return v

    @model_validator(mode="after")
    def _enabled_requires_endpoint(self) -> "LLMConfig":
        if self.enabled and not self.endpoint.strip():
            raise ValueError("llm.enabled=true 时 endpoint 不能为空")
        return self


class EngineConfig(BaseSettings):
    # json_file 不在类定义期固化：settings_customise_sources 每次实例化读模块级 CONFIG_PATH，
    # 测试夹具改 CONFIG_PATH 后重载配置才能生效
    model_config = SettingsConfigDict(json_file_encoding="utf-8", extra="ignore")

    config_version: str = "0.1.0"
    timezone: str
    sample_threshold: SampleThreshold
    wind_bands: list[list[float]]
    mdc: dict[str, MDCSpec]
    mdc_source: str | None = None
    # 报告口径版本（B10/B12 缓存键的一部分）：成绩段口径变更须递增，作废旧口径缓存。
    # 与 mdc_source（MDC 阈值来源，空=降级期）解耦，避免口径升级误触/误退降级期。
    report_caliber_version: str = "v1"
    forbidden_phrases: list[str] = Field(default_factory=list)
    rolling_window_shots: int = Field(gt=0)
    notes_visibility: NotesVisibility
    memory: MemoryConfig = MemoryConfig(
        rolling_snapshot_keep=12,
        anchor_rebuild_policy=AnchorRebuildPolicy(triggers=["season_change", "bow_change", "coach_manual", "four_week_mdc"]),
        notes_display_days=14,
    )
    store: StoreConfig = StoreConfig()
    llm: LLMConfig = LLMConfig()
    log_dir: str = "logs"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 只读 config.json（SSOT），不叠加 env/.env，避免环境变量意外覆盖口径
        # json_file 显式传模块级 CONFIG_PATH：夹具改路径后（monkeypatch 重赋值）立即生效
        return (JsonConfigSettingsSource(settings_cls, json_file=str(CONFIG_PATH), json_file_encoding="utf-8"),)

    @field_validator("timezone")
    @classmethod
    def _tz_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("timezone 不能为空（A1：窗口边界口径）")
        return v

    @field_validator("wind_bands")
    @classmethod
    def _wind_bands_left_closed(cls, v: list[list[float]]) -> list[list[float]]:
        if not v:
            raise ValueError("wind_bands 不能为空")
        prev_hi = None
        for band in v:
            if len(band) != 2 or band[1] <= band[0]:
                raise ValueError(f"风档必须为 [lo, hi) 且 hi>lo：{band}")
            if prev_hi is not None and band[0] != prev_hi:
                raise ValueError("风档必须连续无缝隙（前档 hi == 后档 lo）")
            prev_hi = band[1]
        return v

    @model_validator(mode="after")
    def _mdc_ssot(self) -> "EngineConfig":
        missing = [k for k in REQUIRED_MDC_KEYS if k not in self.mdc]
        if missing:
            raise ValueError(f"mdc 缺必需指标键（D4 SSOT）：{missing}")
        bad = [k for k, s in self.mdc.items() if s.direction not in (-1, 1)]
        if bad:
            raise ValueError(f"mdc 方向必须为 +1/-1：{bad}")
        return self

    def summary(self) -> dict:
        return {
            "config_version": self.config_version,
            "timezone": self.timezone,
            "sample_threshold": self.sample_threshold.model_dump(),
            "wind_bands": self.wind_bands,
            "mdc": {k: v.model_dump() for k, v in self.mdc.items()},
            "mdc_source": self.mdc_source,
            "report_caliber_version": self.report_caliber_version,
            "rolling_window_shots": self.rolling_window_shots,
            "memory": self.memory.model_dump(),
            "llm": {"enabled": self.llm.enabled, "provider": self.llm.provider, "model": self.llm.model},
        }


@lru_cache(maxsize=1)
def get_config() -> EngineConfig:
    return EngineConfig()
