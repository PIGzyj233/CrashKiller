"""Dynamic runtime settings service backed by the system_settings table."""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import utcnow
from app.db.models import SystemSetting
from app.services.llm_providers import (
    LLMProviderId,
    LLMThinkingLevel,
    normalize_provider_id,
    normalize_thinking_level,
)

logger = logging.getLogger(__name__)

AITriageMode = Literal["SMART_TRIAGE", "ALWAYS", "MANUAL"]

DEFAULT_NOISE_MODULES: tuple[str, ...] = (
    "ntdll.dll",
    "kernelbase.dll",
    "kernel32.dll",
    "ucrtbase.dll",
    "ucrtbased.dll",
    "msvcrt.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "vcruntime140d.dll",
    "vcruntime140_1d.dll",
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140d.dll",
    "concrt140.dll",
    "vcomp140.dll",
    "user32.dll",
    "gdi32.dll",
    "win32u.dll",
    "advapi32.dll",
    "ole32.dll",
    "rpcrt4.dll",
    "sechost.dll",
    "wow64.dll",
    "wow64cpu.dll",
    "wow64win.dll",
)

DEFAULT_NOISE_FUNCTIONS: tuple[str, ...] = (
    "KiUserExceptionDispatcher",
    "RtlDispatchException",
    "RtlUserThreadStart",
    "BaseThreadInitThunk",
    "RaiseException",
    "UnhandledExceptionFilter",
    "CxxThrowException",
    "_CxxThrowException",
    "abort",
    "_abort",
    "terminate",
    "std::terminate",
    "_invalid_parameter",
    "_invalid_parameter_noinfo",
    "_invoke_watson",
    "__scrt_common_main_seh",
    "__tmainCRTStartup",
    "std::_X*",
    "__report_gsfailure",
    "__fastfail",
)

SETTINGS_TO_ENV_MAP: dict[str, str] = {
    "llm_provider": "LLM_PROVIDER",
    "llm_base_url": "LLM_BASE_URL",
    "llm_api_key": "LLM_API_KEY",
    "llm_model": "LLM_MODEL",
    "llm_thinking_level": "LLM_THINKING_LEVEL",
    "ai_triage_mode": "AI_TRIAGE_MODE",
    "ai_max_steps": "AI_MAX_STEPS",
    "ai_timeout_seconds": "AI_TIMEOUT_SECONDS",
    "ai_tool_output_max_chars": "AI_TOOL_OUTPUT_MAX_CHARS",
    "total_disk_quota_gb": "TOTAL_DISK_QUOTA_GB",
    "host_safety_floor_gb": "HOST_SAFETY_FLOOR_GB",
    "db_quota_gb": "DB_QUOTA_GB",
    "symbols_sym_quota_gb": "SYMBOLS_SYM_QUOTA_GB",
    "symbols_public_cache_quota_gb": "SYMBOLS_PUBLIC_CACHE_QUOTA_GB",
    "symbols_raw_quota_gb": "SYMBOLS_RAW_QUOTA_GB",
    "logs_quota_gb": "LOGS_QUOTA_GB",
    "dumps_quota_gb": "DUMPS_QUOTA_GB",
    "staging_quota_gb": "STAGING_QUOTA_GB",
    "gc_threshold_percent": "GC_THRESHOLD_PERCENT",
    "log_retention_days": "LOG_RETENTION_DAYS",
    "dump_retention_days": "DUMP_RETENTION_DAYS",
    "staging_ttl_hours": "STAGING_TTL_HOURS",
    "archive_raw_symbols": "ARCHIVE_RAW_SYMBOLS",
    "enable_public_symbols": "ENABLE_PUBLIC_SYMBOLS",
    "public_symbol_urls": "PUBLIC_SYMBOL_URLS",
}

MANAGED_SETTING_KEYS: frozenset[str] = frozenset(
    {
        *SETTINGS_TO_ENV_MAP.keys(),
        "default_noise_modules",
        "default_noise_functions",
    }
)

_PURE_MASK_RE = re.compile(r"^\*{3,}$")

_NUMERIC_SETTING_FIELDS: tuple[str, ...] = (
    "ai_max_steps",
    "ai_timeout_seconds",
    "ai_tool_output_max_chars",
    "total_disk_quota_gb",
    "host_safety_floor_gb",
    "db_quota_gb",
    "symbols_sym_quota_gb",
    "symbols_public_cache_quota_gb",
    "symbols_raw_quota_gb",
    "logs_quota_gb",
    "dumps_quota_gb",
    "staging_quota_gb",
    "gc_threshold_percent",
    "log_retention_days",
    "dump_retention_days",
    "staging_ttl_hours",
)


def _reject_bool_numeric(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("Boolean value is not valid for numeric setting")
    return value


def _clean_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    raise ValueError(f"Expected a list of strings, got {type(value).__name__}")


def mask_api_key(api_key: str | None) -> str | None:
    """Return a non-reversible masked representation of an API key."""
    if not api_key or not api_key.strip():
        return None
    key = api_key.strip()
    if len(key) <= 4:
        return "****"
    if len(key) <= 8:
        return f"{key[0]}***{key[-1]}"
    return f"{key[:3]}****{key[-4:]}"


def is_masked_api_key(candidate: str, current_effective_key: str | None = None) -> bool:
    """Return True if candidate is a masked placeholder rather than a new secret."""
    stripped = candidate.strip()
    if not stripped:
        return False
    if _PURE_MASK_RE.match(stripped):
        return True
    if current_effective_key:
        masked = mask_api_key(current_effective_key)
        if masked is not None and stripped == masked:
            return True
    return False


class EffectiveSettings(BaseModel):
    """Merged runtime settings (env defaults + system_settings DB overrides)."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    llm_provider: LLMProviderId = "openai"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_thinking_level: LLMThinkingLevel = "auto"
    ai_triage_mode: AITriageMode = "SMART_TRIAGE"
    ai_max_steps: int = Field(default=10, ge=1, le=50)
    ai_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    ai_tool_output_max_chars: int = Field(default=8000, ge=100, le=200000)

    total_disk_quota_gb: float = Field(default=150.0, ge=0.0)
    host_safety_floor_gb: float = Field(default=50.0, ge=0.0)
    db_quota_gb: float = Field(default=15.0, ge=0.0)
    symbols_sym_quota_gb: float = Field(default=15.0, ge=0.0)
    symbols_public_cache_quota_gb: float = Field(default=5.0, ge=0.0)
    symbols_raw_quota_gb: float = Field(default=15.0, ge=0.0)
    logs_quota_gb: float = Field(default=55.0, ge=0.0)
    dumps_quota_gb: float = Field(default=35.0, ge=0.0)
    staging_quota_gb: float = Field(default=10.0, ge=0.0)

    gc_threshold_percent: float = Field(default=85.0, gt=0.0, le=100.0)
    log_retention_days: int = Field(default=30, ge=1, le=3650)
    dump_retention_days: int = Field(default=14, ge=1, le=3650)
    staging_ttl_hours: int = Field(default=6, ge=1, le=720)

    archive_raw_symbols: bool = False
    enable_public_symbols: bool = True
    public_symbol_urls: list[str] = Field(default_factory=list)
    default_noise_modules: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NOISE_MODULES),
    )
    default_noise_functions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NOISE_FUNCTIONS),
    )

    @field_validator(*_NUMERIC_SETTING_FIELDS, mode="before")
    @classmethod
    def _reject_bool_on_numeric_fields(cls, value: Any) -> Any:
        return _reject_bool_numeric(value)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _validate_llm_provider(cls, value: Any) -> LLMProviderId:
        return normalize_provider_id(value)

    @field_validator("llm_thinking_level", mode="before")
    @classmethod
    def _validate_llm_thinking_level(cls, value: Any) -> LLMThinkingLevel:
        return normalize_thinking_level(value)

    @field_validator(
        "public_symbol_urls",
        "default_noise_modules",
        "default_noise_functions",
        mode="before",
    )
    @classmethod
    def _validate_str_lists(cls, value: Any) -> list[str]:
        return _clean_str_list(value)

    @property
    def llm_api_key_set(self) -> bool:
        return bool(self.llm_api_key and self.llm_api_key.strip())

    @property
    def llm_api_key_masked(self) -> str | None:
        return mask_api_key(self.llm_api_key)

    def to_app_settings(self, base_settings: Settings | None = None) -> Settings:
        """Return an app.config.Settings copy with all 23 shared DB overrides applied."""
        base = base_settings or get_settings()
        updates: dict[str, Any] = {}
        for snake_key, env_key in SETTINGS_TO_ENV_MAP.items():
            val = getattr(self, snake_key)
            if isinstance(val, list):
                val = list(val)
            updates[env_key] = val
        return base.model_copy(update=updates)

    def to_public_dict(self) -> dict[str, Any]:
        """Return the SystemSettingsRead payload without exposing raw llm_api_key."""
        return {
            "llm_provider": self.llm_provider,
            "llm_base_url": self.llm_base_url,
            "llm_api_key_set": self.llm_api_key_set,
            "llm_api_key_masked": self.llm_api_key_masked,
            "llm_model": self.llm_model,
            "llm_thinking_level": self.llm_thinking_level,
            "ai_triage_mode": self.ai_triage_mode,
            "ai_max_steps": self.ai_max_steps,
            "ai_timeout_seconds": self.ai_timeout_seconds,
            "ai_tool_output_max_chars": self.ai_tool_output_max_chars,
            "total_disk_quota_gb": self.total_disk_quota_gb,
            "host_safety_floor_gb": self.host_safety_floor_gb,
            "db_quota_gb": self.db_quota_gb,
            "symbols_sym_quota_gb": self.symbols_sym_quota_gb,
            "symbols_public_cache_quota_gb": self.symbols_public_cache_quota_gb,
            "symbols_raw_quota_gb": self.symbols_raw_quota_gb,
            "logs_quota_gb": self.logs_quota_gb,
            "dumps_quota_gb": self.dumps_quota_gb,
            "staging_quota_gb": self.staging_quota_gb,
            "gc_threshold_percent": self.gc_threshold_percent,
            "log_retention_days": self.log_retention_days,
            "dump_retention_days": self.dump_retention_days,
            "staging_ttl_hours": self.staging_ttl_hours,
            "archive_raw_symbols": self.archive_raw_symbols,
            "enable_public_symbols": self.enable_public_symbols,
            "public_symbol_urls": list(self.public_symbol_urls),
            "default_noise_modules": list(self.default_noise_modules),
            "default_noise_functions": list(self.default_noise_functions),
        }


class SystemSettingsUpdate(BaseModel):
    """Partial update payload for runtime system settings."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

    llm_provider: LLMProviderId | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_thinking_level: LLMThinkingLevel | None = None
    ai_triage_mode: AITriageMode | None = None
    ai_max_steps: int | None = Field(default=None, ge=1, le=50)
    ai_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    ai_tool_output_max_chars: int | None = Field(default=None, ge=100, le=200000)

    total_disk_quota_gb: float | None = Field(default=None, ge=0.0)
    host_safety_floor_gb: float | None = Field(default=None, ge=0.0)
    db_quota_gb: float | None = Field(default=None, ge=0.0)
    symbols_sym_quota_gb: float | None = Field(default=None, ge=0.0)
    symbols_public_cache_quota_gb: float | None = Field(default=None, ge=0.0)
    symbols_raw_quota_gb: float | None = Field(default=None, ge=0.0)
    logs_quota_gb: float | None = Field(default=None, ge=0.0)
    dumps_quota_gb: float | None = Field(default=None, ge=0.0)
    staging_quota_gb: float | None = Field(default=None, ge=0.0)

    gc_threshold_percent: float | None = Field(default=None, gt=0.0, le=100.0)
    log_retention_days: int | None = Field(default=None, ge=1, le=3650)
    dump_retention_days: int | None = Field(default=None, ge=1, le=3650)
    staging_ttl_hours: int | None = Field(default=None, ge=1, le=720)

    archive_raw_symbols: bool | None = None
    enable_public_symbols: bool | None = None
    public_symbol_urls: list[str] | None = None
    default_noise_modules: list[str] | None = None
    default_noise_functions: list[str] | None = None

    @field_validator(*_NUMERIC_SETTING_FIELDS, mode="before")
    @classmethod
    def _reject_bool_on_numeric_fields(cls, value: Any) -> Any:
        return _reject_bool_numeric(value)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _validate_optional_llm_provider(cls, value: Any) -> LLMProviderId | None:
        if value is None:
            return None
        return normalize_provider_id(value)

    @field_validator("llm_thinking_level", mode="before")
    @classmethod
    def _validate_optional_llm_thinking_level(cls, value: Any) -> LLMThinkingLevel | None:
        if value is None:
            return None
        return normalize_thinking_level(value)

    @field_validator(
        "public_symbol_urls",
        "default_noise_modules",
        "default_noise_functions",
        mode="before",
    )
    @classmethod
    def _validate_optional_str_lists(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        return _clean_str_list(value)


def default_effective_settings(base_settings: Settings | None = None) -> EffectiveSettings:
    """Build EffectiveSettings initialized from environment/config defaults."""
    base = base_settings or get_settings()
    data: dict[str, Any] = {
        snake_key: getattr(base, env_key)
        for snake_key, env_key in SETTINGS_TO_ENV_MAP.items()
    }
    data["default_noise_modules"] = list(DEFAULT_NOISE_MODULES)
    data["default_noise_functions"] = list(DEFAULT_NOISE_FUNCTIONS)
    return EffectiveSettings(**data)


def _unwrap_setting_value(raw: Any) -> Any:
    if isinstance(raw, dict) and set(raw.keys()) == {"value"}:
        return raw["value"]
    return raw


async def get_effective_settings(
    session: AsyncSession,
    base_settings: Settings | None = None,
) -> EffectiveSettings:
    """Load effective settings by merging env defaults with system_settings DB overrides."""
    baseline = default_effective_settings(base_settings)
    merged = baseline.model_dump()

    result = await session.execute(select(SystemSetting))
    rows = result.scalars().all()
    for row in rows:
        if row.key not in MANAGED_SETTING_KEYS:
            continue
        unwrapped = _unwrap_setting_value(row.value_json)
        candidate = dict(merged)
        candidate[row.key] = unwrapped
        try:
            validated = EffectiveSettings.model_validate(candidate)
        except Exception:
            logger.warning(
                "Ignoring invalid persisted system_settings entry for key=%s",
                row.key,
                exc_info=True,
            )
            continue
        merged[row.key] = getattr(validated, row.key)

    return EffectiveSettings.model_validate(merged)


async def get_effective_app_settings(
    session: AsyncSession,
    base_settings: Settings | None = None,
) -> Settings:
    """Convenience helper returning app.config.Settings overlaid with DB overrides."""
    effective = await get_effective_settings(session, base_settings=base_settings)
    return effective.to_app_settings(base_settings=base_settings)


async def update_settings(
    session: AsyncSession,
    updates: SystemSettingsUpdate | dict[str, Any],
    base_settings: Settings | None = None,
) -> EffectiveSettings:
    """Validate and persist partial settings overrides to system_settings."""
    if isinstance(updates, SystemSettingsUpdate):
        payload = updates.model_dump(exclude_unset=True)
    elif isinstance(updates, dict):
        validated_update = SystemSettingsUpdate.model_validate(updates)
        # Preserve only keys originally supplied in the dict.
        raw_keys = set(updates.keys()) & MANAGED_SETTING_KEYS
        payload = {
            k: v
            for k, v in validated_update.model_dump().items()
            if k in raw_keys
        }
    else:
        raise TypeError("updates must be a SystemSettingsUpdate or dict")

    current_effective = await get_effective_settings(session, base_settings=base_settings)

    # Fetch existing rows for upsert.
    existing_result = await session.execute(select(SystemSetting))
    existing_by_key = {row.key: row for row in existing_result.scalars().all()}

    now = utcnow()
    for key, value in payload.items():
        if key not in MANAGED_SETTING_KEYS:
            continue

        if key == "llm_api_key":
            if value is None:
                continue
            str_val = str(value).strip()
            if str_val == "":
                # Persist {"value": ""} when cleared so it overrides env LLM_API_KEY.
                persist_value: Any = ""
            elif is_masked_api_key(str_val, current_effective.llm_api_key):
                continue
            else:
                persist_value = str_val
        else:
            if value is None:
                continue
            persist_value = value

        row = existing_by_key.get(key)
        if row is None:
            row = SystemSetting(
                key=key,
                value_json={"value": persist_value},
                updated_at=now,
            )
            session.add(row)
        else:
            row.value_json = {"value": persist_value}
            row.updated_at = now

    await session.flush()
    await session.commit()
    return await get_effective_settings(session, base_settings=base_settings)


def resolve_project_settings(
    effective: EffectiveSettings,
    project_settings_json: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve per-project overrides on top of effective system settings."""
    proj = project_settings_json if isinstance(project_settings_json, dict) else {}

    triage_mode = effective.ai_triage_mode
    raw_mode = proj.get("ai_triage_mode")
    if isinstance(raw_mode, str) and raw_mode.strip().upper() in {
        "SMART_TRIAGE",
        "ALWAYS",
        "MANUAL",
    }:
        triage_mode = raw_mode.strip().upper()  # type: ignore[assignment]

    replace_defaults = bool(proj.get("replace_default_noise", False))
    base_modules = [] if replace_defaults else list(effective.default_noise_modules)
    base_functions = [] if replace_defaults else list(effective.default_noise_functions)

    extra_modules = _clean_str_list(proj.get("noise_modules"))
    extra_functions = _clean_str_list(proj.get("noise_functions"))
    custom_handlers = _clean_str_list(proj.get("custom_crash_handlers"))

    noise_modules: list[str] = list(dict.fromkeys([*base_modules, *extra_modules]))
    noise_functions: list[str] = list(dict.fromkeys([*base_functions, *extra_functions]))

    for handler in custom_handlers:
        if handler.lower().endswith((".dll", ".exe", ".so", ".dylib")):
            if handler not in noise_modules:
                noise_modules.append(handler)
        if handler not in noise_functions:
            noise_functions.append(handler)

    log_retention_days = effective.log_retention_days
    raw_log_days = proj.get("log_retention_days")
    if not isinstance(raw_log_days, bool) and isinstance(raw_log_days, int) and raw_log_days >= 1:
        log_retention_days = raw_log_days

    dump_retention_days = effective.dump_retention_days
    raw_dump_days = proj.get("dump_retention_days")
    if (
        not isinstance(raw_dump_days, bool)
        and isinstance(raw_dump_days, int)
        and raw_dump_days >= 1
    ):
        dump_retention_days = raw_dump_days

    return {
        "ai_triage_mode": triage_mode,
        "noise_modules": noise_modules,
        "noise_functions": noise_functions,
        "replace_default_noise": replace_defaults,
        "log_retention_days": log_retention_days,
        "dump_retention_days": dump_retention_days,
    }
