from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


class VaultCaptureRequest(BaseModel):
    kind: Literal["note", "bookmark"]
    title: str = Field(min_length=1, max_length=160)
    content: str | None = Field(default=None, max_length=4000)
    url: HttpUrl | None = None

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
