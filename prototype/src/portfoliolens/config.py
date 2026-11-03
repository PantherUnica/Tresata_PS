"""Runtime configuration from environment variables and an optional prototype/.env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

from portfoliolens.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENAI_URL = "https://api.openai.com/v1"


class LLMMode(StrEnum):
    OFFLINE = "offline"
    OPENAI = "openai"
    OPENROUTER = "openrouter"


@dataclass(frozen=True)
class Settings:
    llm_mode: LLMMode = LLMMode.OFFLINE
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str | None = None
    openai_base_url: str = DEFAULT_OPENAI_URL
    openrouter_api_key: str | None = field(default=None, repr=False)
    openrouter_model: str | None = None
    openrouter_base_url: str = DEFAULT_OPENROUTER_URL
    llm_timeout_s: float = 60.0
    near_threshold_band_pp: Decimal = Decimal("1.0")
    identity_min_confidence: float = 0.85
    data_dir: Path = PROJECT_ROOT / "data"
    output_dir: Path = PROJECT_ROOT / "outputs"

    @property
    def llm_label(self) -> str:
        if self.llm_mode is LLMMode.OFFLINE:
            return "offline (deterministic stand-ins, no model calls)"
        if self.llm_mode is LLMMode.OPENAI:
            return f"openai:{self.openai_model}"
        return f"openrouter:{self.openrouter_model}"

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        raw_mode = os.getenv("PL_LLM_MODE", LLMMode.OFFLINE.value).strip().lower()
        try:
            mode = LLMMode(raw_mode)
        except ValueError as exc:
            raise ConfigError(f"PL_LLM_MODE must be one of {[m.value for m in LLMMode]}, got {raw_mode!r}") from exc

        api_key = os.getenv("OPENROUTER_API_KEY", "").strip() or None
        model = os.getenv("OPENROUTER_MODEL", "").strip() or None
        if mode is LLMMode.OPENROUTER and not (api_key and model):
            raise ConfigError("PL_LLM_MODE=openrouter requires OPENROUTER_API_KEY and OPENROUTER_MODEL")

        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip() or None
        openai_model = os.getenv("OPENAI_MODEL", "").strip() or None
        if mode is LLMMode.OPENAI and not (openai_api_key and openai_model):
            raise ConfigError("PL_LLM_MODE=openai requires OPENAI_API_KEY and OPENAI_MODEL")

        return cls(
            llm_mode=mode,
            openai_api_key=openai_api_key,
            openai_model=openai_model,
            openai_base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_URL).strip().rstrip("/"),
            openrouter_api_key=api_key,
            openrouter_model=model,
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_URL).strip().rstrip("/"),
            llm_timeout_s=_float_env("PL_LLM_TIMEOUT_S", 60.0),
            near_threshold_band_pp=_decimal_env("PL_NEAR_THRESHOLD_BAND_PP", Decimal("1.0")),
            identity_min_confidence=_float_env("PL_IDENTITY_MIN_CONFIDENCE", 0.85),
            output_dir=Path(os.getenv("PL_OUTPUT_DIR", str(PROJECT_ROOT / "outputs"))),
        )


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _decimal_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = Decimal(raw.strip())
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a decimal number, got {raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{name} must be a non-negative finite number")
    return value
