from typing import Literal

from pydantic import BaseModel, Field


class VaultSpace(BaseModel):
    kind: Literal["collection", "folder"]
    id: int
    name: str
    color: str | None = Field(default=None, description="Collection accent color")
    icon: str | None = Field(default=None, description="Collection icon name")
    path: str = Field(description="Breadcrumb path, e.g. Work / Project X")
    items_count: int | None = Field(default=None, description="Non-archived items in a collection")


class VaultSpacesRequest(BaseModel):
    include_folders: bool = Field(default=True, description="Include folder nodes as spaces")
    include_archived: bool = Field(default=False, description="Include archived folders")
    collection_id: int | None = Field(default=None, description="Only folders of one collection")


class VaultSpacesResult(BaseModel):
    status: Literal["completed"] = "completed"
    spaces: list[VaultSpace] = Field(default_factory=list)
