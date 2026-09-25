"""Contract for the assistant's own long-lived memory.

Memory is exposed to the agent as ordinary integrations, not as special commands:
whatever the model can call, it can also forget.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

CONTRACT_ID = "miku.memory.v1"

MikuMemoryScope = Literal["profile", "episodic"]
MikuMemoryOperation = Literal["write", "delete"]
MikuMemorySource = Literal["explicit", "derived", "imported"]


class MikuMemoryWriteRequest(BaseModel):
    op: MikuMemoryOperation = "write"
    scope: MikuMemoryScope = "profile"
    key: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = Field(default=None, max_length=500)
    subject: str | None = Field(default=None, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=16)
    source: MikuMemorySource = "explicit"
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: str | None = Field(default=None, max_length=64)

    @field_validator("value")
    @classmethod
    def bound_value(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value).encode()) > 2_048:
            raise ValueError("Memory value is too large")
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized = [tag for item in values if (tag := " ".join(item.split()).casefold())]
        if any(len(tag) > 40 for tag in normalized):
            raise ValueError("Memory tags must not exceed 40 characters")
        return list(dict.fromkeys(normalized))[:16]

    @model_validator(mode="after")
    def validate_payload(self):
        if self.scope == "profile" and not self.key:
            raise ValueError("Profile memory requires a key")
        if self.scope == "episodic" and not self.summary:
            raise ValueError("Episodic memory requires a summary")
        if self.scope == "profile" and self.summary and not self.value:
            self.value = {"summary": self.summary}
        return self


class MikuMemoryWriteResult(BaseModel):
    status: Literal["written", "deleted", "missing"] = "written"
    scope: MikuMemoryScope = "profile"
    key: str | None = None


class MikuMemorySearchRequest(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    scopes: list[MikuMemoryScope] = Field(default_factory=lambda: ["profile", "episodic"], max_length=2)
    limit: int = Field(default=10, ge=1, le=20)


class MikuMemoryEntry(BaseModel):
    scope: MikuMemoryScope
    key: str | None = None
    value: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    subject: str | None = None
    tags: list[str] = Field(default_factory=list)
    source: str = "explicit"
    confidence: float = 1.0
    occurred_at: str | None = None


class MikuMemorySearchResult(BaseModel):
    items: list[MikuMemoryEntry] = Field(default_factory=list, max_length=20)
