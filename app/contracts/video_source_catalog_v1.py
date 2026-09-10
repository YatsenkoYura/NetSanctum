from typing import Literal

from pydantic import BaseModel, Field


class VideoSourceRequest(BaseModel):
    operation: Literal["popular", "subscriptions", "search", "channel", "playlist"] = "popular"
    query: str | None = Field(default=None, max_length=200)
    entity_id: str | None = Field(default=None, max_length=255)
    page_token: str | None = Field(default=None, max_length=500)


class VideoSourceItem(BaseModel):
    entity_type: Literal["youtube_video", "youtube_playlist", "youtube_channel"]
    entity_id: str
    kind: Literal["video", "playlist", "channel"]
    title: str
    description: str = ""
    channel_id: str | None = None
    channel_title: str | None = None
    published_at: str | None = None
    thumbnail_url: str | None = None
    source_url: str
    duration: int | None = None
    view_count: int | None = None


class VideoSourceResult(BaseModel):
    title: str
    items: list[VideoSourceItem]
    next_page_token: str | None = None
