from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MikuQuery(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message must not be blank")
        return value


class MikuProvider(BaseModel):
    module_id: str
    integration_id: str


class MikuReference(BaseModel):
    ref: str
    module_id: str
    item_id: str
    kind: str
    title: str
    subtitle: str | None = None
    summary: str | None = None
    playable: bool = False
    readable: bool = False


class MikuReply(BaseModel):
    command: Literal["help", "sources", "list", "find"]
    text: str
    references: list[MikuReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MikuCapabilities(BaseModel):
    name: str = "MIKU"
    expansion: str = "Miku Is Kernel Utility"
    version: str = "0.1.0"
    mode: Literal["read-only"] = "read-only"
    commands: list[str]
    providers: list[MikuProvider]
