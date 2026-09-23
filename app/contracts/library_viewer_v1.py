from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_ID = "library.viewer.v1"


class LibraryRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"operation": {"const": "detail"}},
                        "required": ["operation"],
                    },
                    "then": {
                        "properties": {"item_id": {"type": "string", "minLength": 1}},
                        "required": ["item_id"],
                    },
                },
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
            ]
        }
    )

    operation: Literal["catalog", "search", "detail"]
    item_id: str | None = Field(default=None, description="Required when operation is detail")
    query: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Case-insensitive title, author, or channel text required for search",
    )
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_operation(self):
        if self.operation == "detail" and not self.item_id:
            raise ValueError("Detail operation requires item_id")
        if self.operation == "search" and not self.query:
            raise ValueError("Search operation requires query")
        return self


class LibraryItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    kind: str
    title: str
    subtitle: str | None = None
    description: str | None = None
    duration: float = Field(default=0, ge=0)
    playable: bool = False
    readable: bool = False
    pages_count: int = Field(default=0, ge=0)
    children: list["LibraryItem"] = Field(default_factory=list)


class LibraryResult(BaseModel):
    module_id: str
    title: str
    order: int
    items: list[LibraryItem] = Field(default_factory=list)
    item: LibraryItem | None = None
    next_offset: int | None = Field(default=None, ge=0)


class LibraryResourceRequest(BaseModel):
    item_id: str
    child_id: str | None = None
    page: int | None = Field(default=None, ge=0)
