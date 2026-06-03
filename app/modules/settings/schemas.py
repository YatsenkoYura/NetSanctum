"""
Settings module — Pydantic request/response schemas.
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SettingScope(str, Enum):
    """Allowed scopes for a setting."""
    GLOBAL = "global"
    MODULE = "module"
    USER = "user"


class ValueType(str, Enum):
    """Supported value types for settings."""
    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    JSON = "json"


# ── Requests ─────────────────────────────────────────────
class SettingCreate(BaseModel):
    """Create or upsert a setting."""
    key: str = Field(..., min_length=1, max_length=255)
    value: str
    scope: SettingScope = SettingScope.GLOBAL
    module_name: str | None = Field(None, max_length=128)
    description: str | None = None
    value_type: ValueType = ValueType.STRING
    is_secret: bool = False


class SettingUpdate(BaseModel):
    """Partial update for an existing setting."""
    value: str | None = None
    description: str | None = None
    value_type: ValueType | None = None
    is_secret: bool | None = None


class SettingBulkCreate(BaseModel):
    """Bulk upsert multiple settings at once."""
    settings: list[SettingCreate]


# ── Responses ────────────────────────────────────────────
class SettingResponse(BaseModel):
    """Single setting response (secret values are masked)."""
    id: int
    scope: str
    module_name: str | None
    user_id: int | None
    key: str
    value: str
    description: str | None
    value_type: str
    is_secret: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SettingListResponse(BaseModel):
    """Paginated list of settings."""
    items: list[SettingResponse]
    total: int
    page: int
    page_size: int


class SettingResolvedResponse(BaseModel):
    """
    A resolved setting value — after applying the scope hierarchy
    (user → module → global fallback).
    """
    key: str
    value: str
    value_type: str
    resolved_scope: str
    source_id: int
