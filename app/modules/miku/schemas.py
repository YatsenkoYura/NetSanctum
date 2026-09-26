from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

MikuCommand = Literal["respond", "open", "play", "find", "list", "discover"]
MikuMemorySource = Literal["explicit", "derived", "imported"]
MikuMood = Literal["neutral", "happy", "confused", "thinking", "listening"]


def strip_emoji(value: str) -> str:
    return "".join(
        character
        for character in value
        if not (
            0x1F000 <= ord(character) <= 0x1FAFF
            or 0x2600 <= ord(character) <= 0x27BF
            or ord(character) in {0x200D, 0x20E3, 0xFE0E, 0xFE0F}
        )
    )


class MikuQuery(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)
    context_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # A stored thread. When present the transcript is the history the model is given
    # and both sides of the exchange are written back to it.
    conversation_id: int | None = Field(default=None, ge=1)

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
    contract: str = "library.viewer.v1"


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
    entity_type: str | None = None
    open_url: str | None = None
    resource_url: str | None = None

    @field_validator("title", "subtitle", "summary")
    @classmethod
    def remove_emoji(cls, value: str | None, info):
        if value is None:
            return None
        value = " ".join(strip_emoji(value).split())
        if value:
            return value
        return "Untitled" if info.field_name == "title" else None


class MikuJobStatus(BaseModel):
    task_id: str
    module_id: str
    status: str
    progress: str | None = None
    title: str | None = None


class MikuReplySegment(BaseModel):
    kind: Literal["acknowledgement", "response", "status"]
    text: str = Field(min_length=1, max_length=500)
    # Opt-out, not opt-in: a segment is spoken unless something decides otherwise.
    # Defaulting this to False silenced every reply, because nothing in the codebase
    # ever set it and the client reads the flag rather than assuming speech - the
    # segments it invents for itself default to True, so the two sides disagreed and
    # the server's answer won. Anything that should only be shown says so here.
    speak: bool = True

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = " ".join(strip_emoji(value).split())
        if not value:
            raise ValueError("Reply segment must not be blank")
        return value


class MikuReply(BaseModel):
    command: MikuCommand
    text: str = Field(max_length=500)
    mood: MikuMood = "neutral"
    segments: list[MikuReplySegment] = Field(default_factory=list, max_length=4)
    references: list[MikuReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    client_action: Literal["open", "play"] | None = None
    question: str | None = Field(default=None, max_length=300)
    question_options: list[str] = Field(default_factory=list, max_length=4)
    exhausted: bool = False

    @model_validator(mode="after")
    def populate_legacy_segment(self):
        self.text = " ".join(strip_emoji(self.text).split())
        if not self.text:
            raise ValueError("Reply text must not be blank")
        if not self.segments:
            self.segments = [MikuReplySegment(kind="response", text=self.text)]
        return self


class MikuCapabilities(BaseModel):
    name: str = "MIKU"
    expansion: str = "Miku Is Kernel Utility"
    version: str = "0.3.0"
    mode: Literal["guarded"] = "guarded"
    transport: Literal["rest+websocket"] = "rest+websocket"
    protocol_version: int = 3
    commands: list[str]
    providers: list[MikuProvider]


class MikuSocketMessage(BaseModel):
    type: Literal["query", "ping", "cancel", "voice", "speak"]
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    message: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)
    audio: str | None = Field(default=None, max_length=6_000_000)
    audio_content_type: str | None = Field(default=None, max_length=64)
    conversation_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_payload(self):
        if self.type in {"query", "speak"}:
            self.message = (self.message or "").strip()
            if not self.message:
                raise ValueError("Query message must not be blank")
        elif self.type == "voice":
            if not self.audio:
                raise ValueError("Voice message requires audio")
            if not self.audio_content_type:
                raise ValueError("Voice message requires an audio content type")
        elif self.message is not None:
            raise ValueError("Ping and cancel do not accept a message")
        if self.type != "voice" and (self.audio is not None or self.audio_content_type is not None):
            raise ValueError("Only voice messages accept audio")
        return self


class MikuSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str = Field(default="alloy", min_length=1, max_length=64)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = " ".join(strip_emoji(value).split())
        if not value:
            raise ValueError("Speech text must not be blank")
        return value


MikuProviderMode = Literal["api", "local", "client"]


class MikuRuntimeCapabilities(BaseModel):
    enabled: bool
    llm: bool = False
    stt: bool = False
    tts: bool = False
    modes: dict[str, MikuProviderMode] = Field(default_factory=dict)


class MikuProviderInput(BaseModel):
    url: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=200)
    api_key: str = Field(default="", max_length=4096)
    mode: MikuProviderMode = "api"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Provider URL must be HTTP or HTTPS")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Provider URL must not contain credentials or a fragment")
        return value.rstrip("/")

    @field_validator("model", "api_key")
    @classmethod
    def strip_provider_value(cls, value: str) -> str:
        return value.strip()


class MikuProviderSettingsUpdate(BaseModel):
    llm: MikuProviderInput
    stt: MikuProviderInput
    tts: MikuProviderInput

    @model_validator(mode="after")
    def validate_modes(self):
        if self.llm.mode == "client":
            raise ValueError("Chat provider cannot run on the client")
        return self


class MikuProviderStatus(BaseModel):
    url: str = ""
    model: str = ""
    api_key_set: bool = False
    mode: MikuProviderMode = "api"


class MikuProviderSettingsResponse(BaseModel):
    llm: MikuProviderStatus
    stt: MikuProviderStatus
    tts: MikuProviderStatus


class MikuSessionMemory(BaseModel):
    summary: str | None = Field(default=None, max_length=500)
    recent_topics: list[str] = Field(default_factory=list, max_length=12)
    active_references: list[MikuReference] = Field(default_factory=list, max_length=20)
    ttl_seconds: int = Field(default=900, ge=60, le=86_400)

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(strip_emoji(value).split())
        return value or None

    @field_validator("recent_topics")
    @classmethod
    def normalize_recent_topics(cls, values: list[str]) -> list[str]:
        normalized = [topic for item in values if (topic := " ".join(item.split()))]
        if any(len(topic) > 80 for topic in normalized):
            raise ValueError("Recent topics must not exceed 80 characters")
        return list(dict.fromkeys(normalized))


class MikuProfileMemoryItem(BaseModel):
    memory_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$")
    value: dict[str, Any] = Field(default_factory=dict)
    source: MikuMemorySource = "explicit"
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: str | None = Field(default=None, max_length=64)


class MikuEpisodeMemoryItem(BaseModel):
    summary: str = Field(min_length=1, max_length=500)
    subject: str | None = Field(default=None, max_length=160)
    tags: list[str] = Field(default_factory=list, max_length=16)
    source_module_id: str | None = Field(default=None, max_length=63)
    source_item_id: str | None = Field(default=None, max_length=255)
    source: MikuMemorySource = "derived"
    occurred_at: str | None = Field(default=None, max_length=64)

    @field_validator("summary", "subject")
    @classmethod
    def normalize_episode_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(strip_emoji(value).split())
        if not value:
            return None
        return value

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized = [tag for item in values if (tag := " ".join(item.split()).casefold())]
        if any(len(tag) > 40 for tag in normalized):
            raise ValueError("Episode memory tags must not exceed 40 characters")
        return list(dict.fromkeys(normalized))


class MikuMemorySnapshot(BaseModel):
    session: MikuSessionMemory = Field(default_factory=MikuSessionMemory)
    profile: list[MikuProfileMemoryItem] = Field(default_factory=list, max_length=64)
    episodic: list[MikuEpisodeMemoryItem] = Field(default_factory=list, max_length=64)


class MikuConversationTurn(BaseModel):
    user: str = Field(min_length=1, max_length=500)
    assistant: str = Field(min_length=1, max_length=500)


class MikuConversationCreate(BaseModel):
    title: str = Field(default="", max_length=120)


class MikuConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class MikuConversationSummary(BaseModel):
    id: int
    title: str
    message_count: int = 0
    created_at: str
    updated_at: str


class MikuConversationList(BaseModel):
    items: list[MikuConversationSummary] = Field(default_factory=list, max_length=100)


class MikuStoredMessage(BaseModel):
    id: int
    role: str
    content: str
    command: str | None = None
    created_at: str


class MikuConversationDetail(BaseModel):
    conversation: MikuConversationSummary
    messages: list[MikuStoredMessage] = Field(default_factory=list, max_length=200)


class MikuConversationNoteItem(BaseModel):
    key: str
    value: dict = Field(default_factory=dict)
    updated_at: str


class MikuConversationNoteList(BaseModel):
    items: list[MikuConversationNoteItem] = Field(default_factory=list, max_length=50)
