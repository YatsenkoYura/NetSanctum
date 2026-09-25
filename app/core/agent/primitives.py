"""Universal agent primitives.

The assistant must not be limited to a fixed list of app commands. It gets a small
set of capabilities it can compose: read what a search returned, fetch a public URL,
act on the user's screen, ask, and finish. Everything else arrives automatically from
the active module integrations.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

READ_TEXT_LIMIT = 8_000
FETCH_TEXT_LIMIT = 12_000
FETCH_MAX_BYTES = 4 * 1024 * 1024
MAX_URL_LENGTH = 2_000
MAX_REF_LENGTH = 200
MAX_QUESTION_LENGTH = 300
MAX_OPTION_LENGTH = 80
MAX_QUESTION_OPTIONS = 4
MAX_ANSWER_LENGTH = 2_000
MAX_ANSWER_REFS = 20
MAX_SCRATCHPAD_LENGTH = 600
MIN_READ_CHARS = 256


class AgentReadRequest(BaseModel):
    ref: str = Field(
        min_length=1,
        max_length=MAX_REF_LENGTH,
        description="Result reference from an earlier step, for example result:1.",
    )
    max_chars: int = Field(
        default=READ_TEXT_LIMIT,
        ge=MIN_READ_CHARS,
        le=READ_TEXT_LIMIT,
        description="How much content to return. The engine truncates anyway.",
    )


class AgentReadResult(BaseModel):
    ref: str
    kind: str
    title: str
    text: str = ""
    truncated: bool = False
    pages_count: int | None = None


class AgentFetchRequest(BaseModel):
    url: str = Field(
        min_length=8,
        max_length=MAX_URL_LENGTH,
        description="Public https URL to read. Private and loopback addresses are refused.",
    )
    max_chars: int = Field(
        default=FETCH_TEXT_LIMIT,
        ge=MIN_READ_CHARS,
        le=FETCH_TEXT_LIMIT,
    )


class AgentFetchResult(BaseModel):
    url: str
    final_url: str
    title: str = ""
    content_type: str = ""
    text: str = ""
    truncated: bool = False
    bytes_read: int = 0


class AgentActRequest(BaseModel):
    ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    action: Literal["open", "play"] = Field(description="Open the item on screen, or start playing it.")


class AgentActResult(BaseModel):
    ref: str
    action: Literal["open", "play"]
    target: str = ""
    ok: bool = True


class AgentAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    options: list[str] = Field(default_factory=list, max_length=MAX_QUESTION_OPTIONS)


class AgentFinalRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=MAX_ANSWER_LENGTH)
    refs: list[str] = Field(
        default_factory=list,
        max_length=MAX_ANSWER_REFS,
        description="References actually used, so the user can open them.",
    )


class AgentSpeechRequest(BaseModel):
    """Speech synthesis input, bounded like every other primitive."""

    text: str = Field(min_length=1, max_length=2_000)
    voice: str = Field(min_length=1, max_length=64, default="alloy")


class AgentStep(BaseModel):
    """One model decision inside a cascade: a tool call, a question, or the final answer."""

    scratchpad: str | None = Field(default=None, max_length=MAX_SCRATCHPAD_LENGTH)
    kind: Literal["tool", "ask", "final"] = "tool"
    tool: str | None = Field(default=None, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    # A model may ask for several things at once; the engine runs them one by one.
    queued: list[dict[str, Any]] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_payload(self):
        if self.kind == "tool":
            if not self.tool:
                raise ValueError("A tool step requires a tool name")
            if not self.arguments:
                raise ValueError("A tool step requires arguments")
        elif self.kind == "ask":
            AgentAskRequest.model_validate(self.arguments)
        else:
            AgentFinalRequest.model_validate(self.arguments)
        return self


PRIMITIVE_NAMES: tuple[str, ...] = ("read", "fetch", "act", "ask", "final")
