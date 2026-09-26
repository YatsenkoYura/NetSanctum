"""Contract for what the agent carries forward inside one conversation.

The model is given a short window of recent turns, not the whole transcript. These
notes are how it remembers the rest on its own terms: it decides what still matters
and writes it down, rather than the application replaying the whole conversation
into every prompt.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

CONTRACT_ID = "miku.conversation_note.v1"

MikuNoteOperation = Literal["write", "delete"]


class MikuNoteWriteRequest(BaseModel):
    op: MikuNoteOperation = "write"
    key: str | None = Field(default=None, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: dict[str, Any] = Field(default_factory=dict)
    text: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_payload(self):
        if not self.key:
            raise ValueError("A note needs a key")
        if self.op == "write" and not self.value and not self.text:
            raise ValueError("A note needs a value or text")
        if self.text and not self.value:
            self.value = {"text": self.text}
        if len(str(self.value).encode()) > 2_048:
            raise ValueError("Note value is too large")
        return self


class MikuNoteWriteResult(BaseModel):
    status: Literal["written", "deleted", "missing"] = "written"
    key: str | None = None


class MikuNoteSearchRequest(BaseModel):
    query: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=20)


class MikuNoteEntry(BaseModel):
    key: str
    value: dict[str, Any] = Field(default_factory=dict)


class MikuNoteSearchResult(BaseModel):
    items: list[MikuNoteEntry] = Field(default_factory=list, max_length=20)
