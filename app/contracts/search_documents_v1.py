from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

CONTRACT_ID = "search.documents.v1"


class SearchDocumentsRequest(BaseModel):
    offset: int = Field(default=0, ge=0, le=100_000)
    limit: int = Field(default=250, ge=1, le=500)


class SearchDocument(BaseModel):
    document_id: str = Field(min_length=1, max_length=255)
    entity_type: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1, max_length=255)
    subtitle: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=4000)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    updated_at: datetime | None = None
    open_path: str | None = Field(default=None, max_length=1000)
    playable: bool = False
    readable: bool = False

    @field_validator("title", "subtitle", "body")
    @classmethod
    def normalize_text(cls, value: str | None, info):
        if value is None:
            return None
        value = " ".join(value.split())
        if info.field_name == "title" and not value:
            raise ValueError("Search document title must not be blank")
        return value or None

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]):
        normalized = list(dict.fromkeys(value for item in values if (value := " ".join(item.split()))))
        if any(len(value) > 100 for value in normalized):
            raise ValueError("Search document keywords must not exceed 100 characters")
        return normalized

    @field_validator("open_path")
    @classmethod
    def validate_open_path(cls, value: str | None):
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            not value.startswith("/")
            or value.startswith("//")
            or parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or "\\" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("Search document open_path must be a local absolute URL")
        return value


class SearchDocumentsResult(BaseModel):
    module_id: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    documents: list[SearchDocument] = Field(default_factory=list, max_length=500)
    next_offset: int | None = Field(default=None, ge=0, le=100_000)


class SearchDocumentResourceRequest(BaseModel):
    document_id: str = Field(min_length=1, max_length=255)
    resource_kind: Literal["primary"] = "primary"
