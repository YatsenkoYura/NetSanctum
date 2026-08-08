import datetime
from typing import Any

from pydantic import BaseModel, Field


class VaultCollectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    color: str = "teal"
    icon: str | None = None


class VaultCollectionResponse(BaseModel):
    id: int
    name: str
    description: str | None = None
    color: str
    icon: str | None = None
    created_at: datetime.datetime
    items_count: int | None = 0

    class Config:
        from_attributes = True


class VaultItemCreate(BaseModel):
    entry_type: str = Field("bookmark", description="bookmark, rating, or thought")
    title: str | None = Field(default="", max_length=1000)
    content: str | None = None
    url: str | None = None

    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None

    score: float | None = Field(None, ge=1.0, le=10.0)
    status: str | None = Field(None, description="watching, completed, dropped, planned, on_hold")
    progress_current: int = 0
    progress_total: int | None = None
    rewatch_count: int = 0
    category: str | None = Field(None, description="anime, series, movie, game, manga, book, article, other")

    tags: list[str] = Field(default_factory=list)
    is_pinned: bool = False
    is_archived: bool = False
    collection_id: int | None = None

    related_entity_type: str | None = None
    related_entity_id: str | None = None

    parent_id: int | None = None
    is_folder: bool = False
    node_type: str = "note"  # folder, note, table, whiteboard, bookmark, rating
    canvas_data: dict[str, Any] = Field(default_factory=dict)

    auto_fetch_og: bool = True  # If true and url provided, fetch OG metadata


class VaultItemUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    url: str | None = None
    score: float | None = None
    status: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    rewatch_count: int | None = None
    category: str | None = None
    tags: list[str] | None = None
    is_pinned: bool | None = None
    is_archived: bool | None = None
    collection_id: int | None = None
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    parent_id: int | None = None
    is_folder: bool | None = None
    node_type: str | None = None
    canvas_data: dict[str, Any] | None = None


class VaultItemResponse(BaseModel):
    id: int
    entry_type: str
    title: str
    content: str | None = None
    url: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None
    score: float | None = None
    status: str | None = None
    progress_current: int
    progress_total: int | None = None
    rewatch_count: int
    category: str | None = None
    tags: list[str]
    is_pinned: bool
    is_archived: bool
    collection_id: int | None = None
    collection_name: str | None = None
    related_entity_type: str | None = None
    related_entity_id: str | None = None
    parent_id: int | None = None
    is_folder: bool = False
    node_type: str = "note"
    canvas_data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime.datetime
    updated_at: datetime.datetime

    class Config:
        from_attributes = True


class VaultStatsResponse(BaseModel):
    total_items: int
    bookmarks_count: int
    ratings_count: int
    thoughts_count: int
    completed_count: int
    watching_count: int
    pinned_count: int
    archived_count: int
    avg_score: float | None = 0.0
    categories_breakdown: dict[str, int]
    top_tags: list[dict[str, Any]]
