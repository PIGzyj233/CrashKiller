"""Pydantic request and response schemas for CrashKiller API v1."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_project_settings_json(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    cleaned = dict(value)
    if "session_id_label" in cleaned:
        raw_label = cleaned["session_id_label"]
        if raw_label is None:
            cleaned.pop("session_id_label", None)
        elif not isinstance(raw_label, str):
            raise ValueError("session_id_label must be a string")
        else:
            if any(ch in raw_label for ch in ("\r", "\n", "\x00")):
                raise ValueError("session_id_label must not contain control characters")
            stripped = raw_label.strip()
            if not stripped:
                cleaned.pop("session_id_label", None)
            elif len(stripped) > 64:
                raise ValueError("session_id_label must not exceed 64 characters")
            else:
                cleaned["session_id_label"] = stripped
    return cleaned


class ProjectCreate(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    settings_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("key", "name")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field must not be empty or whitespace")
        return cleaned

    @field_validator("settings_json")
    @classmethod
    def _validate_settings(cls, value: dict[str, Any]) -> dict[str, Any]:
        validated = _validate_project_settings_json(value)
        return validated if validated is not None else {}


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    settings_json: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Project name must not be empty")
        return cleaned

    @field_validator("settings_json")
    @classmethod
    def _validate_optional_settings(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _validate_project_settings_json(value)


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    settings_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ProjectCreateResponse(ProjectRead):
    api_key: str


class ProjectRotateKeyResponse(BaseModel):
    id: uuid.UUID
    key: str
    api_key: str


class ProjectApiKeyResponse(BaseModel):
    id: uuid.UUID
    key: str
    api_key: str | None = None


class ProjectApiKeySyncRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=255)

    @field_validator("api_key")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("API key must not be empty")
        return cleaned
