from typing import Literal

from pydantic import BaseModel, Field


class ArchiveVideoRequest(BaseModel):
    entity_type: str = Field(min_length=1, max_length=64)
    entity_id: str = Field(min_length=1, max_length=255)
    quality: Literal["best", "1080", "720", "480", "360"] = "720"


class ArchiveVideoResult(BaseModel):
    status: Literal["dispatched"]
    task_id: str
    platform: str
    message: str
