from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MikuCommand = Literal["help", "sources", "list", "find", "repeat"]


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
    command: MikuCommand
    text: str
    references: list[MikuReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MikuCapabilities(BaseModel):
    name: str = "MIKU"
    expansion: str = "Miku Is Kernel Utility"
    version: str = "0.1.0"
    mode: Literal["read-only"] = "read-only"
    transport: Literal["rest+websocket"] = "rest+websocket"
    protocol_version: int = 1
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

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message must not be blank")
        return value


class MikuDecision(BaseModel):
    command: MikuCommand
    argument: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_command(self):
        self.argument = self.argument.strip()
        if self.command == "find" and not self.argument:
            raise ValueError("The find command requires search text")
        if self.command in {"help", "sources", "repeat"} and self.argument:
            raise ValueError(f"The {self.command} command does not accept arguments")
        return self
