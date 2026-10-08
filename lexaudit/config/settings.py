"""Application settings — the single source of configuration (GLOBAL RULES, working rule 6).

All values come from environment variables / ``.env`` (see ``.env.example``).
Modules read these settings; they never hardcode configuration values.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root: <root>/lexaudit/config/settings.py -> two levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Typed LexAudit settings. Field name == environment variable name."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- Alem.ai (OpenAI-compatible LLM endpoint) ----------------------------
    ALEM_API_KEY: SecretStr = SecretStr("")
    # Alem Plus documents this OpenAI-compatible endpoint.
    ALEM_BASE_URL: str = "https://llm.alem.ai/v1"
    ALEM_MODEL: str = "qwen3-8"
    LLM_TOOL_MODE: str = "auto"
    LLM_RETRIES: int = 3
    LLM_RETRY_BACKOFF_SECONDS: float = 1.0
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_LOG_CONTENT_LIMIT: int = 2000

    # Legal-agent orchestration.
    AGENT_MODE: str = "per_block"
    AGENT_MAX_STEPS: int = 6
    AGENT_CONTEXT_SUMMARY_LIMIT: int = 300
    AGENT_TRACE_SUMMARY_LIMIT: int = 300
    AGENT_LAW_CONTEXT_ARTICLES: int = 5
    AGENT_LAW_CONTEXT_MAX_CHARS: int = 12000
    SECTION_MAX_CHARS: int = 6000
    EVIDENCE_CONCURRENCY: int = 6
    EVIDENCE_ACTS: int = 2
    VERDICT_CONCURRENCY: int = 6

    # HTTP service and document uploads.
    PIPELINE_MODE: str = "inline"
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    API_MAX_WORKERS: int = 2
    PIPELINE_OUTPUT_DIR: Path = PROJECT_ROOT / "out"

    # -- Adilet (adilet.zan.kz) legal-information API -------------------------
    # Canonical public base used for document URLs in SearchHit.url.
    ADILET_BASE_URL: str = "https://adilet.zan.kz"
    # Legacy portal that serves the anonymous HTML search endpoint
    # (the new SPA's /api/documents/search requires a user token).
    # See docs/adilet-api.md.
    ADILET_SEARCH_BASE_URL: str = "https://old.adilet.zan.kz"
    # TTL in days for the SQLite search_cache table (T1.2). 0 disables caching.
    SEARCH_CACHE_TTL_DAYS: float = 7.0
    # Refresh interval for mirrored Adilet act text.
    ACT_REFRESH_DAYS: float = 90.0

    # -- Storage ----------------------------------------------------------------
    DATABASE_PATH: Path = PROJECT_ROOT / "data" / "lexaudit.db"
    LOG_DIR: Path = PROJECT_ROOT / "logs"

    # -- Prompts / observability --------------------------------------------------
    PROMPTS_VERSION: str = "v1"
    LOG_LEVEL: str = "INFO"

    @field_validator("DATABASE_PATH", "LOG_DIR", "PIPELINE_OUTPUT_DIR", mode="after")
    @classmethod
    def _resolve_relative(cls, value: Path) -> Path:
        """Relative paths from .env are resolved against the project root."""
        return value if value.is_absolute() else (PROJECT_ROOT / value)


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance (the environment is read once per process)."""
    return Settings()
