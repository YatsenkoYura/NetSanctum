from pydantic import BaseModel, Field, field_validator


class MessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    recipient_id: str | None = None
    audience: str = "player"

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Сообщение не может быть пустым")
        return value


class ParticipantUpdate(BaseModel):
    is_alive: bool | None = None
    seat: int | None = Field(default=None, ge=0, le=99)
    reminders: list[str] | None = Field(default=None, max_length=20)
    gm_notes: str | None = Field(default=None, max_length=2000)


class ParticipantSwap(BaseModel):
    target_id: str = Field(min_length=36, max_length=36)
