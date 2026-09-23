from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class VideoSourceRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"operation": {"const": "search"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {"query": {"type": "string", "minLength": 1}},
                        "required": ["query"],
                    },
                },
                {
                    "if": {
                        "properties": {"operation": {"enum": ["channel", "playlist"]}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {"entity_id": {"type": "string", "minLength": 1}},
                        "required": ["entity_id"],
                    },
                },
            ]
        }
    )

    operation: Literal[
        "recommendations",
        "popular",
        "subscriptions",
        "history",
        "watch_later",
        "search",
        "channel",
        "playlist",
    ] = "recommendations"
    query: str | None = Field(default=None, max_length=200, description="Required for search")
    entity_id: str | None = Field(
        default=None,
        max_length=255,
        description="Required for channel or playlist",
    )
    page_token: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_operation(self):
        if self.operation == "search" and not self.query:
            raise ValueError("Search requires a query")
        if self.operation in {"channel", "playlist"} and not self.entity_id:
            raise ValueError(f"{self.operation.title()} requires an entity ID")
        return self


class VideoSourceItem(BaseModel):
    entity_type: Literal["youtube_video", "youtube_playlist", "youtube_channel"]
    entity_id: str
    kind: Literal["video", "playlist", "channel"]
    title: str
    description: str = ""
    channel_id: str | None = None
    channel_title: str | None = None
    channel_avatar_url: str | None = None
    published_at: str | None = None
    thumbnail_url: str | None = None
    source_url: str
    duration: int | None = None
    view_count: int | None = None


class VideoSourceResult(BaseModel):
    title: str
    items: list[VideoSourceItem]
    next_page_token: str | None = None
    description: str = ""
    thumbnail_url: str | None = None
    avatar_url: str | None = None
    source_url: str | None = None
