"""Turn integration results into agent references.

Only shared contracts are understood here, so the engine never imports a module and a
new module becomes usable by the agent as soon as it implements a known contract.
"""

from typing import Any

from pydantic import BaseModel, Field

MAX_TITLE_LENGTH = 160
MAX_SUMMARY_LENGTH = 400
MAX_REFERENCES = 20

SEARCH_CONTRACT = "search.query.v1"
LIBRARY_CONTRACT = "library.viewer.v1"
SOURCE_CONTRACT = "video.source.catalog.v1"


class AgentReference(BaseModel):
    """Something the agent found and may later read, mention or act on."""

    ref: str = Field(pattern=r"^result:([1-9]|1[0-9]|20)$", max_length=12)
    module_id: str = Field(max_length=63)
    item_id: str = Field(max_length=200)
    kind: str = Field(max_length=64, default="item")
    title: str = Field(max_length=MAX_TITLE_LENGTH)
    subtitle: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    summary: str | None = Field(default=None, max_length=MAX_SUMMARY_LENGTH)
    entity_type: str | None = Field(default=None, max_length=64)
    playable: bool = False
    readable: bool = False
    open_url: str | None = Field(default=None, max_length=1000)

    @property
    def resource_endpoint(self) -> str | None:
        if not (self.playable or self.readable):
            return None
        return f"/api/miku/resources/{self.module_id}/{self.item_id}"


def _clip(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit] or None


def project_references(
    contract: str | None,
    module_id: str,
    result: dict[str, Any],
    *,
    limit: int = MAX_REFERENCES,
) -> list[AgentReference]:
    """Project one integration result into references, numbered for the model."""
    references: list[AgentReference] = []

    if contract == SEARCH_CONTRACT:
        for item in (result.get("items") or [])[:limit]:
            item_module = str(item.get("source_module_id") or module_id)[:63]
            references.append(
                AgentReference(
                    ref=f"result:{len(references) + 1}",
                    module_id=item_module,
                    item_id=str(item.get("document_id") or "")[:200],
                    kind=str(item.get("entity_type") or "item")[:64],
                    title=_clip(item.get("title"), MAX_TITLE_LENGTH) or "Untitled",
                    subtitle=_clip(item.get("subtitle"), MAX_TITLE_LENGTH),
                    summary=_clip(item.get("summary"), MAX_SUMMARY_LENGTH),
                    entity_type=_clip(item.get("entity_type"), 64),
                    playable=bool(item.get("playable")),
                    readable=bool(item.get("readable")),
                    open_url=_clip(item.get("open_path"), 1000),
                )
            )
        return references

    if contract == LIBRARY_CONTRACT:
        items = result.get("items") or ([result["item"]] if result.get("item") else [])
        for item in items[:limit]:
            playable = bool(item.get("playable"))
            readable = bool(item.get("readable"))
            references.append(
                AgentReference(
                    ref=f"result:{len(references) + 1}",
                    module_id=str(result.get("module_id") or module_id)[:63],
                    item_id=str(item.get("id") or "")[:200],
                    kind=str(item.get("kind") or "item")[:64],
                    title=_clip(item.get("title"), MAX_TITLE_LENGTH) or "Untitled",
                    subtitle=_clip(item.get("subtitle"), MAX_TITLE_LENGTH),
                    summary=_clip(item.get("description"), MAX_SUMMARY_LENGTH),
                    entity_type=_clip(item.get("kind"), 64),
                    playable=playable,
                    readable=readable,
                    open_url=(
                        f"/api/miku/resources/{result.get('module_id') or module_id}/{item.get('id')}"
                        if playable or readable
                        else None
                    ),
                )
            )
        return references

    if contract == SOURCE_CONTRACT:
        for item in (result.get("items") or [])[:limit]:
            kind = str(item.get("kind") or "item")[:64]
            references.append(
                AgentReference(
                    ref=f"result:{len(references) + 1}",
                    module_id=str(result.get("module_id") or module_id)[:63],
                    item_id=str(item.get("entity_id") or "")[:200],
                    kind=kind,
                    title=_clip(item.get("title"), MAX_TITLE_LENGTH) or "Untitled",
                    subtitle=_clip(item.get("channel_title"), MAX_TITLE_LENGTH),
                    summary=_clip(item.get("description"), MAX_SUMMARY_LENGTH),
                    entity_type=_clip(item.get("entity_type"), 64),
                    playable=kind == "video",
                    open_url=(f"/youtube/watch/{item.get('entity_id')}" if kind == "video" else None),
                )
            )
        return references

    return references


__all__ = [
    "LIBRARY_CONTRACT",
    "MAX_REFERENCES",
    "SEARCH_CONTRACT",
    "SOURCE_CONTRACT",
    "AgentReference",
    "project_references",
]
