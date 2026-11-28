"""Application configuration loaded from environment and .env."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, SecretStr
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _parse_str_list(value: Any) -> list[str]:
    """Parse JSON array strings, comma-separated strings, or lists into list[str]."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            parsed = json.loads(stripped)
            if not isinstance(parsed, list):
                raise ValueError("Expected a JSON list for list setting")
            return [str(item).strip() for item in parsed if str(item).strip()]
        if stripped.startswith("{"):
            raise ValueError("Expected a JSON list or comma-separated string, not JSON object")
        return [part.strip() for part in stripped.split(",") if part.strip()]
    raise ValueError(f"Cannot parse string list from {type(value).__name__}")


StrList = Annotated[list[str], NoDecode, BeforeValidator(_parse_str_list)]


class Settings(BaseSettings):
    """Typed runtime configuration loaded from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application & Web Server ---
    APP_NAME: str = "CrashKiller"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: StrList = []
    HTTP_PORT: int = 8080
    UVICORN_WORKERS: int = 2
    DASHBOARD_CACHE_TTL_SECONDS: int = 0
    ADMIN_USERNAME: str = ""
    ADMIN_PASSWORD: SecretStr = SecretStr("")
    ALLOW_UNAUTHENTICATED_SYMSRV: bool = False

    # --- PostgreSQL ---
    POSTGRES_USER: str = "crashkiller"
    POSTGRES_PASSWORD: str = "crashkiller"
    POSTGRES_DB: str = "crashkiller"
    DATABASE_URL: str = (
        "postgresql+asyncpg://crashkiller:crashkiller@postgres:5432/crashkiller"
    )
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10

    # --- Redis & Workers ---
    REDIS_URL: str = "redis://redis:6379/0"
    SPLIT_WORKER_QUEUES: bool = False
    COMPUTE_WORKER_MAX_JOBS: int = 4
    IO_WORKER_MAX_JOBS: int = 16

    # --- Host Storage & Partition Layout ---
    CRASHKILLER_DATA_PATH: str = "./data"
    DATA_DIR: str = "/data"

    # --- Compact Disk Quotas & Safety Floor (GB / Days) ---
    TOTAL_DISK_QUOTA_GB: float = 150.0
    HOST_SAFETY_FLOOR_GB: float = 50.0

    DB_QUOTA_GB: float = 15.0
    SYMBOLS_SYM_QUOTA_GB: float = 15.0
    SYMBOLS_PUBLIC_CACHE_QUOTA_GB: float = 5.0
    SYMBOLS_RAW_QUOTA_GB: float = 15.0
    LOGS_QUOTA_GB: float = 55.0
    DUMPS_QUOTA_GB: float = 35.0
    STAGING_QUOTA_GB: float = 10.0

    GC_THRESHOLD_PERCENT: float = 85.0
    LOG_RETENTION_DAYS: int = 30
    DUMP_RETENTION_DAYS: int = 14
    STAGING_TTL_HOURS: int = 6
    ARCHIVE_RAW_SYMBOLS: bool = False

    # --- Native Symbol & MiniDump Toolchain ---
    DUMP_SYMS_BIN: str = "dump_syms"
    MINIDUMP_STACKWALK_BIN: str = "minidump-stackwalk"
    CABEXTRACT_BIN: str = "cabextract"
    ENABLE_PUBLIC_SYMBOLS: bool = True
    PUBLIC_SYMBOL_URLS: StrList = [
        "https://symbols.mozilla.org/",
        "https://msdl.microsoft.com/download/symbols/",
    ]
    TOOL_TIMEOUT_SECONDS: int = 120
    MS_SYMBOL_AUTO_FETCH: bool = True
    MS_SYMBOL_FETCH_PE_UNWIND: bool = True
    MS_SYMBOL_FETCH_TIMEOUT_SECONDS: int = 30
    MS_SYMBOL_MAX_MODULES_PER_CRASH: int = 16
    MS_SYMBOL_MAX_PDB_MB: int = 64
    MS_SYMBOL_MAX_PE_MB: int = 32
    MS_SYMBOL_NEGATIVE_CACHE_HOURS: int = 24
    MS_SYMBOL_AUTO_RESYMBOLICATE: bool = True
    MS_SYMBOL_ENABLE_WINBINDEX_FALLBACK: bool = True

    # --- Chunked Zstd Compression ---
    LOG_CHUNK_SIZE_BYTES: int = 262144
    LOG_ZSTD_LEVEL: int = 6
    DUMP_ZSTD_LEVEL: int = 3

    # --- AI Agent & Multi-Provider LLM Configuration ---
    LLM_PROVIDER: Literal[
        "openai",
        "anthropic",
        "gemini",
        "deepseek",
        "openrouter",
        "qwen",
        "zhipu",
        "ollama",
        "custom",
    ] = "openai"
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_THINKING_LEVEL: Literal["auto", "off", "low", "medium", "high"] = "auto"
    AI_TRIAGE_MODE: Literal["SMART_TRIAGE", "ALWAYS", "MANUAL"] = "SMART_TRIAGE"
    AI_OUTPUT_LANGUAGE: Literal["zh-CN", "en-US"] = "zh-CN"
    AI_MAX_STEPS: int = 10
    AI_TIMEOUT_SECONDS: int = 120
    AI_TOOL_OUTPUT_MAX_CHARS: int = 8000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings singleton."""
    return Settings()
