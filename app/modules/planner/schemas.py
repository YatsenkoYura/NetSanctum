import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

DEFAULT_TZ = ZoneInfo("Europe/Moscow")

Recurrence = Literal["none", "daily", "weekdays", "weekly", "monthly"]
SpaceKind = Literal["none", "collection", "folder"]
TaskStatus = Literal["todo", "doing", "done", "cancelled"]
EventStatus = Literal["active", "done", "cancelled"]


def _as_utc(value: datetime.datetime | None) -> datetime.datetime | None:
    """Model datetimes may arrive naive; a bare wall time means the owner's clock."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=DEFAULT_TZ)
    return value.astimezone(datetime.UTC)


class SpaceMixin(BaseModel):
    space_kind: SpaceKind = Field(default="none", description="none, collection, or folder")
    space_id: int | None = Field(default=None, description="Vault collection or folder id")
    space_name: str | None = Field(default=None, max_length=120, description="Cached space name for badges")


class TaskCreate(SpaceMixin):
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    priority: int = Field(default=0, ge=0, le=3, description="0 normal, 3 most urgent")
    due_at: datetime.datetime | None = Field(default=None, description="ISO datetime, naive means Moscow")
    remind_at: datetime.datetime | None = Field(default=None, description="When to push a reminder")
    recurrence: Recurrence = Field(default="none", description="Repeat rule for the next instance")
    raw_text: str | None = Field(default=None, max_length=255, description="Original wording, for display")

    @field_validator("due_at", "remind_at", mode="after")
    @classmethod
    def _as_utc_field(cls, value):
        return _as_utc(value)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    priority: int | None = Field(default=None, ge=0, le=3)
    status: TaskStatus | None = None
    due_at: datetime.datetime | None = None
    remind_at: datetime.datetime | None = None
    recurrence: Recurrence | None = None
    space_kind: SpaceKind | None = None
    space_id: int | None = None
    space_name: str | None = Field(default=None, max_length=120)
    raw_text: str | None = Field(default=None, max_length=255)

    @field_validator("due_at", "remind_at", mode="after")
    @classmethod
    def _as_utc_field(cls, value):
        return _as_utc(value)


class TaskResponse(SpaceMixin):
    id: int
    title: str
    notes: str | None = None
    status: str
    priority: int
    due_at: datetime.datetime | None = None
    remind_at: datetime.datetime | None = None
    recurrence: str
    raw_text: str | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime
    completed_at: datetime.datetime | None = None

    class Config:
        from_attributes = True


class EventCreate(SpaceMixin):
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=200)
    starts_at: datetime.datetime = Field(description="ISO datetime, naive means Moscow")
    ends_at: datetime.datetime | None = None
    remind_at: datetime.datetime | None = Field(default=None, description="When to push a reminder")
    recurrence: Recurrence = Field(default="none", description="Repeat rule for the next instance")
    raw_text: str | None = Field(default=None, max_length=255, description="Original wording, for display")

    @field_validator("starts_at", "ends_at", "remind_at", mode="after")
    @classmethod
    def _as_utc_field(cls, value):
        return _as_utc(value)


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=200)
    status: EventStatus | None = None
    starts_at: datetime.datetime | None = None
    ends_at: datetime.datetime | None = None
    remind_at: datetime.datetime | None = None
    recurrence: Recurrence | None = None
    space_kind: SpaceKind | None = None
    space_id: int | None = None
    space_name: str | None = Field(default=None, max_length=120)
    raw_text: str | None = Field(default=None, max_length=255)

    @field_validator("starts_at", "ends_at", "remind_at", mode="after")
    @classmethod
    def _as_utc_field(cls, value):
        return _as_utc(value)


class EventResponse(SpaceMixin):
    id: int
    title: str
    notes: str | None = None
    location: str | None = None
    status: str
    starts_at: datetime.datetime
    ends_at: datetime.datetime | None = None
    remind_at: datetime.datetime | None = None
    recurrence: str
    raw_text: str | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    class Config:
        from_attributes = True
