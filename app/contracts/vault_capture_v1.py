from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class VaultCaptureRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"kind": {"const": "note"}},
                        "required": ["kind"],
                    },
                    "then": {
                        "properties": {"content": {"type": "string", "minLength": 1}},
                        "required": ["content"],
                    },
                },
                {
                    "if": {
                        "properties": {"kind": {"const": "bookmark"}},
                        "required": ["kind"],
                    },
                    "then": {
                        "properties": {"url": {"type": "string", "format": "uri"}},
                        "required": ["url"],
                    },
                },
            ]
        }
    )

    kind: Literal["note", "bookmark"]
    title: str = Field(min_length=1, max_length=160)
    content: str | None = Field(default=None, max_length=4000, description="Required for a note")
    url: HttpUrl | None = Field(default=None, description="Required for a bookmark")

    @model_validator(mode="after")
    def validate_kind(self):
        if self.kind == "note" and not self.content:
            raise ValueError("A note requires content")
        if self.kind == "bookmark" and not self.url:
            raise ValueError("A bookmark requires an HTTP URL")
        if self.url and (self.url.username or self.url.password):
            raise ValueError("Bookmark URLs must not contain credentials")
        return self


class VaultCaptureResult(BaseModel):
    status: Literal["completed"] = "completed"
    item_id: int
    kind: Literal["note", "bookmark"]
    title: str
    message: str
