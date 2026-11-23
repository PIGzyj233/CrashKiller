"""Pydantic request and response schemas for CrashKiller API v1."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
