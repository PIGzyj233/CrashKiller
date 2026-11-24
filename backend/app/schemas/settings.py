"""Pydantic schemas for runtime system settings and storage garbage collection."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.llm_providers import LLMProtocol, LLMProviderId, LLMThinkingLevel
from app.services.settings_service import SystemSettingsUpdate

AITriageModeLiteral = Literal["SMART_TRIAGE", "ALWAYS", "MANUAL"]
AIOutputLanguageLiteral = Literal["zh-CN", "en-US"]


class LLMProviderPresetRead(BaseModel):
    id: LLMProviderId
    name: str
    protocol: LLMProtocol
    default_base_url: str
    default_model: str
    models: list[str] = Field(default_factory=list)
    requires_api_key: bool
    supports_thinking: bool


class SystemSettingsRead(BaseModel):
    llm_provider: LLMProviderId
    llm_base_url: str
    llm_api_key_set: bool
    llm_api_key_masked: str | None = None
    llm_model: str
    llm_thinking_level: LLMThinkingLevel
    ai_triage_mode: AITriageModeLiteral
    ai_output_language: AIOutputLanguageLiteral = "zh-CN"
    ai_max_steps: int
    ai_timeout_seconds: int
    ai_tool_output_max_chars: int
    total_disk_quota_gb: float
    host_safety_floor_gb: float
    db_quota_gb: float
    symbols_sym_quota_gb: float
    symbols_public_cache_quota_gb: float
    symbols_raw_quota_gb: float
    logs_quota_gb: float
    dumps_quota_gb: float
    staging_quota_gb: float
    gc_threshold_percent: float
    log_retention_days: int
    dump_retention_days: int
    staging_ttl_hours: int
    archive_raw_symbols: bool
    enable_public_symbols: bool
    public_symbol_urls: list[str] = Field(default_factory=list)
    default_noise_modules: list[str] = Field(default_factory=list)
    default_noise_functions: list[str] = Field(default_factory=list)


class GCRequest(BaseModel):
    force_emergency: bool = False


class GCReport(BaseModel):
    freed_bytes: int
    deleted_files_count: int
    skipped_pinned_count: int
    emergency_triggered: bool
    partitions: dict[str, Any]


__all__ = [
    "AIOutputLanguageLiteral",
    "AITriageModeLiteral",
    "GCReport",
    "GCRequest",
    "LLMProviderPresetRead",
    "SystemSettingsRead",
    "SystemSettingsUpdate",
]
