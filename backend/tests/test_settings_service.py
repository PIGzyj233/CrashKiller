"""Unit tests for runtime settings_service overrides and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import SystemSetting
from app.services import settings_service


async def test_settings_defaults_partial_update_and_api_key_lifecycle(
    db_session: AsyncSession,
    tmp_data_dir: Path,
) -> None:
    base = Settings(
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        DATA_DIR=str(tmp_data_dir),
        LLM_API_KEY="sk-env-secret-9876",
        ARCHIVE_RAW_SYMBOLS=False,
    )
    eff = await settings_service.get_effective_settings(db_session, base_settings=base)
    assert eff.llm_api_key == "sk-env-secret-9876"
    assert eff.llm_api_key_set is True
    assert eff.llm_api_key_masked is not None
    assert "9876" in eff.llm_api_key_masked
    assert "sk-env-secret-9876" not in eff.llm_api_key_masked
    assert "llm_api_key" not in eff.to_public_dict()
    assert eff.default_noise_modules == list(settings_service.DEFAULT_NOISE_MODULES)
    assert eff.default_noise_functions == list(settings_service.DEFAULT_NOISE_FUNCTIONS)

    # Partial update preserves llm_api_key when omitted, None, or masked.
    eff2 = await settings_service.update_settings(
        db_session,
        {
            "llm_model": "gpt-4o",
            "archive_raw_symbols": True,
            "host_safety_floor_gb": 25.0,
            "llm_api_key": eff.llm_api_key_masked,
        },
        base_settings=base,
    )
    assert eff2.llm_model == "gpt-4o"
    assert eff2.llm_api_key == "sk-env-secret-9876"
    assert eff2.archive_raw_symbols is True
    app_cfg = eff2.to_app_settings(base_settings=base)
    assert app_cfg.ARCHIVE_RAW_SYMBOLS is True
    assert app_cfg.HOST_SAFETY_FLOOR_GB == 25.0
    assert app_cfg.LLM_MODEL == "gpt-4o"

    # Setting llm_api_key="" persists {"value": ""} and overrides non-empty env LLM_API_KEY.
    eff_cleared = await settings_service.update_settings(
        db_session,
        {"llm_api_key": ""},
        base_settings=base,
    )
    assert eff_cleared.llm_api_key == ""
    assert eff_cleared.llm_api_key_set is False
    assert eff_cleared.llm_api_key_masked is None


async def test_settings_validation_corrupt_row_and_project_resolution(
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(total_disk_quota_gb=-1.0)
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(gc_threshold_percent=0.0)
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(ai_triage_mode="INVALID_MODE")  # type: ignore[arg-type]

    # Legacy scalar + corrupt DB row should not crash get_effective_settings.
    db_session.add(SystemSetting(key="ai_max_steps", value_json=5))
    db_session.add(SystemSetting(key="gc_threshold_percent", value_json={"value": -99}))
    await db_session.commit()

    eff = await settings_service.get_effective_settings(db_session, base_settings=test_settings)
    assert eff.ai_max_steps == 5
    assert eff.gc_threshold_percent == test_settings.GC_THRESHOLD_PERCENT

    proj_cfg = settings_service.resolve_project_settings(
        eff,
        {
            "ai_triage_mode": "manual",
            "noise_modules": ["CustomWrap.dll"],
            "custom_crash_handlers": ["crashpad.dll", "ReportCrash"],
        },
    )
    assert proj_cfg["ai_triage_mode"] == "MANUAL"
    assert proj_cfg["noise_modules"]
    assert "ReportCrash" in proj_cfg["noise_functions"]

    # Non-finite floats must be rejected by both SystemSettingsUpdate and EffectiveSettings.
    for bad_float in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValidationError):
            settings_service.SystemSettingsUpdate(total_disk_quota_gb=bad_float)
        with pytest.raises(ValidationError):
            settings_service.EffectiveSettings(total_disk_quota_gb=bad_float)

    # Booleans must not be coerced to int retention days in resolve_project_settings.
    bool_proj_cfg = settings_service.resolve_project_settings(
        eff,
        {"log_retention_days": True, "dump_retention_days": False},
    )
    assert bool_proj_cfg["log_retention_days"] == eff.log_retention_days
    assert bool_proj_cfg["dump_retention_days"] == eff.dump_retention_days


async def test_settings_rejects_booleans_on_all_numeric_fields(
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    numeric_fields = (
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
    for field in numeric_fields:
        for bool_val in (True, False):
            with pytest.raises(ValidationError):
                settings_service.SystemSettingsUpdate.model_validate({field: bool_val})
            with pytest.raises(ValidationError):
                settings_service.EffectiveSettings.model_validate({field: bool_val})

    # update_settings must reject booleans on numeric fields before persisting anything.
    baseline = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    for key, bad_val in (
        ("log_retention_days", True),
        ("dump_retention_days", False),
        ("total_disk_quota_gb", False),
        ("gc_threshold_percent", True),
        ("ai_max_steps", True),
        ("staging_ttl_hours", True),
    ):
        with pytest.raises(ValidationError):
            await settings_service.update_settings(
                db_session,
                {key: bad_val},
                base_settings=test_settings,
            )

    after_rejected = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert after_rejected.log_retention_days == baseline.log_retention_days
    assert after_rejected.total_disk_quota_gb == baseline.total_disk_quota_gb
    assert after_rejected.gc_threshold_percent == baseline.gc_threshold_percent

    # Legitimate boolean fields continue accepting bool values.
    updated_bools = await settings_service.update_settings(
        db_session,
        {"archive_raw_symbols": True, "enable_public_symbols": False},
        base_settings=test_settings,
    )
    assert updated_bools.archive_raw_symbols is True
    assert updated_bools.enable_public_symbols is False

    # Corrupt boolean DB rows on numeric keys must be skipped and fall back to env defaults.
    db_session.add(SystemSetting(key="log_retention_days", value_json={"value": True}))
    db_session.add(SystemSetting(key="total_disk_quota_gb", value_json=False))
    db_session.add(SystemSetting(key="gc_threshold_percent", value_json={"value": True}))
    await db_session.commit()

    recovered = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert recovered.log_retention_days == test_settings.LOG_RETENTION_DAYS
    assert recovered.total_disk_quota_gb == test_settings.TOTAL_DISK_QUOTA_GB
    assert recovered.gc_threshold_percent == test_settings.GC_THRESHOLD_PERCENT


async def test_settings_llm_provider_and_thinking_level_lifecycle(
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    baseline = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert baseline.llm_provider == "openai"
    assert baseline.llm_thinking_level == "auto"
    public = baseline.to_public_dict()
    assert public["llm_provider"] == "openai"
    assert public["llm_thinking_level"] == "auto"

    # Valid update with mixed case normalizes to lowercase literals.
    updated = await settings_service.update_settings(
        db_session,
        {
            "llm_provider": "Anthropic",
            "llm_base_url": "https://api.anthropic.com",
            "llm_model": "claude-sonnet-4-5",
            "llm_thinking_level": "HIGH",
        },
        base_settings=test_settings,
    )
    assert updated.llm_provider == "anthropic"
    assert updated.llm_model == "claude-sonnet-4-5"
    assert updated.llm_thinking_level == "high"
    app_cfg = updated.to_app_settings(base_settings=test_settings)
    assert app_cfg.LLM_PROVIDER == "anthropic"
    assert app_cfg.LLM_THINKING_LEVEL == "high"

    # Reject unknown provider or thinking level.
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(llm_provider="unknown_vendor")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(llm_thinking_level="extreme")  # type: ignore[arg-type]

    # Corrupt DB values fall back cleanly to defaults.
    await db_session.merge(SystemSetting(key="llm_provider", value_json={"value": "bad_vendor"}))
    await db_session.merge(
        SystemSetting(key="llm_thinking_level", value_json={"value": "bad_level"})
    )
    await db_session.commit()

    recovered = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert recovered.llm_provider == test_settings.LLM_PROVIDER
    assert recovered.llm_thinking_level == test_settings.LLM_THINKING_LEVEL


async def test_settings_ai_output_language_lifecycle(
    db_session: AsyncSession,
    test_settings: Settings,
) -> None:
    baseline = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert baseline.ai_output_language == "zh-CN"
    assert baseline.to_public_dict()["ai_output_language"] == "zh-CN"

    updated_en = await settings_service.update_settings(
        db_session,
        {"ai_output_language": "en_US"},
        base_settings=test_settings,
    )
    assert updated_en.ai_output_language == "en-US"
    assert updated_en.to_app_settings(base_settings=test_settings).AI_OUTPUT_LANGUAGE == "en-US"

    updated_zh = await settings_service.update_settings(
        db_session,
        {"ai_output_language": "zh"},
        base_settings=test_settings,
    )
    assert updated_zh.ai_output_language == "zh-CN"

    with pytest.raises(ValidationError):
        settings_service.SystemSettingsUpdate(ai_output_language="fr-FR")  # type: ignore[arg-type]

    await db_session.merge(
        SystemSetting(key="ai_output_language", value_json={"value": "invalid-lang"})
    )
    await db_session.commit()
    recovered = await settings_service.get_effective_settings(
        db_session, base_settings=test_settings
    )
    assert recovered.ai_output_language == test_settings.AI_OUTPUT_LANGUAGE
