import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MikuCommand = Literal[
    "help",
    "sources",
    "list",
    "find",
    "repeat",
    "discover",
    "open",
    "play",
    "archive",
    "note",
    "bookmark",
    "invoke",
    "respond",
]


class MikuQuery(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)
    context_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

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


class MikuPendingAction(BaseModel):
    action: Literal["archive", "note", "bookmark", "invoke"]
    label: str
    summary: str
    confirmation_token: str


class MikuActionConfirmation(BaseModel):
    confirmation_token: str = Field(min_length=32, max_length=4096)


class MikuActionResult(BaseModel):
    status: Literal["dispatched", "completed"]
    message: str
    task_id: str | None = None


class MikuJobStatus(BaseModel):
    task_id: str
    module_id: str
    status: str
    progress: str | None = None
    title: str | None = None


class MikuReply(BaseModel):
    command: MikuCommand
    text: str
    references: list[MikuReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    client_action: Literal["open", "play"] | None = None
    pending_action: MikuPendingAction | None = None


class MikuCapabilities(BaseModel):
    name: str = "MIKU"
    expansion: str = "Miku Is Kernel Utility"
    version: str = "0.2.0"
    mode: Literal["guarded"] = "guarded"
    transport: Literal["rest+websocket"] = "rest+websocket"
    protocol_version: int = 2
    commands: list[str]
    providers: list[MikuProvider]


class MikuSocketMessage(BaseModel):
    type: Literal["query", "ping"]
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    message: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def validate_payload(self):
        if self.type == "query":
            self.message = (self.message or "").strip()
            if not self.message:
                raise ValueError("Query message must not be blank")
        elif self.message is not None:
            raise ValueError("Ping does not accept a message")
        return self


class MikuDecisionRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    tools: list["MikuToolDefinition"] = Field(default_factory=list, max_length=20)
    context: list["MikuContextReference"] = Field(default_factory=list, max_length=20)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message must not be blank")
        return value


class MikuSpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str = Field(default="alloy", min_length=1, max_length=64)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Speech text must not be blank")
        return value


class MikuRuntimeCapabilities(BaseModel):
    enabled: bool
    llm: bool = False
    stt: bool = False
    tts: bool = False


class MikuToolDefinition(BaseModel):
    integration_id: str = Field(max_length=128, pattern=r"^[a-z][a-z0-9_.-]*\.v[1-9][0-9]*$")
    module_id: str = Field(max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    contract: str | None = None
    effect: Literal["read", "create", "update", "delete", "execute"]
    description: str = Field(max_length=300)
    input_schema: dict[str, Any]


class MikuContextReference(BaseModel):
    ref: str = Field(pattern=r"^result:([1-9]|1[0-9]|20)$")
    module_id: str = Field(max_length=63)
    item_id: str = Field(max_length=255)
    entity_type: str | None = Field(default=None, max_length=64)
    kind: str = Field(max_length=64)
    title: str = Field(max_length=160)
    playable: bool = False
    readable: bool = False


class MikuDecision(BaseModel):
    command: MikuCommand
    argument: str = Field(default="", max_length=500)
    integration_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]*\.v[1-9][0-9]*$",
        description="Exact API ID from the supplied catalog, or null for assistant-only commands",
    )
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_command(self):
        self.argument = self.argument.strip()
        if self.command in {"open", "play", "archive", "note", "bookmark", "respond"} and not self.argument:
            raise ValueError(f"The {self.command} command requires an argument")
        if self.command in {"help", "sources", "repeat"} and self.argument:
            raise ValueError(f"The {self.command} command does not accept arguments")
        if self.command == "invoke" and not self.integration_id:
            raise ValueError("The invoke command requires an integration ID")
        if self.command != "invoke" and (self.integration_id or self.parameters):
            raise ValueError(f"The {self.command} command does not select a module integration")
        if len(self.parameters) > 20 or len(json.dumps(self.parameters)) > 4096:
            raise ValueError("Integration parameters are too large")
        return self


MikuDecisionRequest.model_rebuild()
