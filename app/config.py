# -*- coding: utf-8 -*-
"""口径配置加载（config.json → pydantic 模型，严格校验，缺键/方向错误启动失败）。

口径来源：
- 工程化方案 D7（timezone/sample_threshold/wind_bands）、D4.1（mdc/mdc_source）、D4（forbidden_phrases）
- 记忆系统方案 §3.1（rolling_window_shots/notes_visibility）
- 阶段 1：data_quality / advice / report_caliber_version / change_caliber_trial（master）
- PR 栈：portal / security / conclusions / llm 流式超时 / store.id_secret_path（portal-p0）
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


class ChangeBand(BaseModel):
    """天花板效应分区间：按当前值落入的档位覆盖 te/swc（未给出则继承指标默认）。"""

    max: float
    te: float | None = None
    swc: float | None = None


class MDCSpec(BaseModel):
    """指标变化判定口径（Hopkins TE/SWC 两级 + 天花板分区间）。

    - te：典型误差（噪声底）——变化 ≤ te 视为不可判定；
    - swc：最小有价值变化——te < 变化 ≤ swc 视为正常波动；
    - threshold：旧单阈值口径，未配置 te/swc 的指标回退使用；
    - bands：按当前值分档覆盖 te/swc，应对高分区噪声骤降（天花板效应）。
    强断言门槛 MDC95 由 te 派生（2.772 × te），不入配置（见 rules.MDC95_FACTOR）。
    """

    direction: int = Field(ge=-1, le=1)
    threshold: float | None = None
    te: float | None = None
    swc: float | None = None
    bands: list[ChangeBand] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bands_strictly_ascending(self) -> "MDCSpec":
        maxes = [b.max for b in self.bands]
        if maxes != sorted(maxes) or len(set(maxes)) != len(maxes):
            raise ValueError(f"bands 必须按 max 严格升序：{maxes}")
        return self


class DataQuality(BaseModel):
    """数据质量护栏（P0）：对不可能的分布与过小样本显式标注「数据存疑」。"""

    min_shots_for_description: int = Field(gt=0)
    score_max: float = Field(gt=0)


class Advice(BaseModel):
    """建议层（P0）：由判断信号生成确定性训练建议的触发阈值。"""

    miss_rate_high: float = Field(ge=0, le=100)
    far_miss_high: float = Field(ge=0, le=100)
    inner10_low: float = Field(ge=0, le=100)
    wind_band_delta: float = Field(gt=0)
    min_band_shots: int = Field(gt=0)


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
    sqlite_min_time: str = "2024-01-01"
    session_gap_minutes: int = Field(default=90, gt=0)
    default_distance_m: int = Field(default=70, gt=0)
    shot_type_map: dict[str, int] = Field(default_factory=lambda: {"1": 0, "3": 1, "7": 1})
    project_bow_map: dict[str, str] = Field(default_factory=lambda: {"171": "反曲弓"})
    # P0 隐私（portal）：athlete_id 脱敏密钥文件
    id_secret_path: str | None = None
    # v1.2 接口 bowType 英文枚举 → 引擎内部弓种值
    v12_bow_type_map: dict[str, str] = Field(default_factory=lambda: {"recurve": "反曲弓", "compound": "复合弓"})


class LLMConfig(BaseModel):
    """对话增强（B1）+ 流式 AI 训练指导（PR #3）。"""

    enabled: bool = False
    provider: str = "llamacpp"
    endpoint: str = "http://127.0.0.1:8080"
    model: str = "qwen2.5-1.5b-instruct-q4_k_m"
    model_version: str = "qwen2.5-1.5b-v1"
    timeout_sec: int = Field(default=180, gt=0)
    max_retries: int = Field(default=1, ge=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    num_ctx: int = Field(default=2048, gt=0)
    connect_timeout_sec: float = Field(default=5.0, gt=0)
    first_token_timeout_sec: float = Field(default=90, gt=0)
    idle_timeout_sec: float = Field(default=30, gt=0)
    total_timeout_sec: float = Field(default=180, gt=0)
    guidance_max_len: int = Field(default=1000, gt=0)

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


class PortalConfig(BaseModel):
    """PR #4：同源托管服务中心门户（不开 CORS）。"""

    dist_dir: str | None = None


class SecurityConfig(BaseModel):
    """PR #4：跨站写请求拦截。"""

    block_cross_site_writes: bool = True
    allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("allowed_origins")
    @classmethod
    def _no_wildcard_or_null(cls, v: list[str]) -> list[str]:
        out = []
        for o in v:
            o = o.strip().rstrip("/")
            if o in ("*", "null", ""):
                raise ValueError("security.allowed_origins 不允许 '*'、'null' 或空串（null origin 等于任何网站）")
            out.append(o.lower())
        return out


class ConclusionsConfig(BaseModel):
    """PR #4：引擎结论口径（风档差 / 平台期）。"""

    wind_gap_split_mps: float = Field(default=2.0, gt=0)
    wind_gap_min_shots: int = Field(default=5, ge=1)
    wind_gap_threshold: float = Field(default=0.3, ge=0)
    plateau_periods: int = Field(default=3, ge=2)
    plateau_metric: str = "avgScore"
    provisional: bool = True

    @field_validator("plateau_metric")
    @classmethod
    def _plateau_metric_known(cls, v: str) -> str:
        if v not in REQUIRED_MDC_KEYS:
            raise ValueError(f"conclusions.plateau_metric 必须为 {REQUIRED_MDC_KEYS}")
        return v


class EngineConfig(BaseSettings):
    model_config = SettingsConfigDict(json_file_encoding="utf-8", extra="ignore")

    config_version: str = "0.1.0"
    timezone: str
    sample_threshold: SampleThreshold
    wind_bands: list[list[float]]
    mdc: dict[str, MDCSpec]
    mdc_source: str | None = None
    change_caliber_trial: bool = False
    report_caliber_version: str = "v1"
    data_quality: DataQuality
    advice: Advice
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
    portal: PortalConfig = PortalConfig()
    security: SecurityConfig = SecurityConfig()
    conclusions: ConclusionsConfig = ConclusionsConfig()
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
            "change_caliber_trial": self.change_caliber_trial,
            "data_quality": self.data_quality.model_dump(),
            "advice": self.advice.model_dump(),
            "report_caliber_version": self.report_caliber_version,
            "rolling_window_shots": self.rolling_window_shots,
            "memory": self.memory.model_dump(),
            "llm": {"enabled": self.llm.enabled, "provider": self.llm.provider, "model": self.llm.model},
        }


@lru_cache(maxsize=1)
def get_config() -> EngineConfig:
    return EngineConfig()
