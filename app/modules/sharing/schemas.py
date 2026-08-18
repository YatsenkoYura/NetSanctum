from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ShareCreate(BaseModel):
    module_id: str = Field(min_length=1, max_length=63)
    title: str = Field(min_length=1, max_length=255)
    selection_mode: Literal["all", "selected"] = "selected"
    selector: dict = Field(default_factory=dict)
    public: bool = False
    password: str | None = None
    expires_at: datetime | None = None
    allow_download: bool = False

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Title must not be empty")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must not exceed 72 UTF-8 bytes")
        return value

    @field_validator("expires_at")
    @classmethod
    def normalize_expiration(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
