import asyncio
import datetime
import logging
import re
from typing import Any
from urllib.parse import urljoin

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.modules import module_registry
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked, validate_remote_url
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.schemas import (
    VaultCollectionCreate,
    VaultItemCreate,
    VaultItemUpdate,
)

logger = logging.getLogger(__name__)


async def _is_public_http_url(url: str) -> bool:
    try:
        await asyncio.to_thread(validate_remote_url, url)
    except (RemoteFetchError, OSError):
        return False
    return True


async def _fetch_public_html(url: str, headers: dict[str, str]) -> str | None:
    try:
        content, _content_type, _final_url = await asyncio.to_thread(
            fetch_bytes_checked,
            url,
            headers=headers,
            max_redirects=4,
            max_bytes=150000,
            allowed_content_prefixes=("text/html", "application/xhtml+xml"),
        )
    except (RemoteFetchError, OSError):
        return None
    return content.decode("utf-8", errors="replace")


async def fetch_url_metadata(url: str) -> dict[str, str | None]:
    """
    Asynchronously scrape OpenGraph metadata (og:title, og:description, og:image)
    and standard HTML title from a web URL.
    """
    result: dict[str, str | None] = {"og_title": None, "og_description": None, "og_image": None}
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        return result

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    }

    try:
        html = await _fetch_public_html(url, headers)
        if html is None:
            return result

        # 1. Parse og:title or fallback to <title>
        og_title_match = re.search(
            r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE
        )
        if og_title_match:
            result["og_title"] = og_title_match.group(1).strip()
        else:
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            if title_match:
                result["og_title"] = title_match.group(1).strip()

        # 2. Parse og:description or fallback to meta name="description"
        og_desc_match = re.search(
            r'<meta\s+property=["\']og:description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE
        )
        if og_desc_match:
            result["og_description"] = og_desc_match.group(1).strip()
        else:
            desc_match = re.search(
                r'<meta\s+name=["\']description["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE
            )
            if desc_match:
                result["og_description"] = desc_match.group(1).strip()

        # 3. Parse og:image
        og_img_match = re.search(
            r'<meta\s+property=["\']og:image["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE
        )
        if og_img_match:
            result["og_image"] = urljoin(url, og_img_match.group(1).strip())

    except Exception as e:
        logger.debug("Failed to fetch OG metadata for %s: %s", url, e)

    return result


async def create_vault_item(session: AsyncSession, item_in: VaultItemCreate) -> VaultItem:
    """Create a new vault entry (bookmark, rating, thought)."""
    og_meta = {}
    if item_in.url and item_in.auto_fetch_og:
        og_meta = await fetch_url_metadata(item_in.url)

    item = VaultItem(
        entry_type=item_in.entry_type,
        title=item_in.title if item_in.title is not None else "",
        content=item_in.content,
        url=item_in.url,
        og_title=item_in.og_title or og_meta.get("og_title"),
        og_description=item_in.og_description or og_meta.get("og_description"),
        og_image=item_in.og_image or og_meta.get("og_image"),
        score=item_in.score,
        status=item_in.status,
        progress_current=item_in.progress_current or 0,
        progress_total=item_in.progress_total,
        rewatch_count=item_in.rewatch_count or 0,
        category=item_in.category,
        tags=item_in.tags or [],
        is_pinned=item_in.is_pinned,
        is_archived=item_in.is_archived,
        collection_id=item_in.collection_id,
        related_entity_type=item_in.related_entity_type,
        related_entity_id=item_in.related_entity_id,
        parent_id=item_in.parent_id,
        is_folder=item_in.is_folder,
        node_type=item_in.node_type or "note",
        canvas_data=item_in.canvas_data or {},
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def get_vault_item(session: AsyncSession, item_id: int) -> VaultItem | None:
    """Get single item by ID."""
    stmt = select(VaultItem).where(VaultItem.id == item_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def update_vault_item(session: AsyncSession, item: VaultItem, update_in: VaultItemUpdate) -> VaultItem:
    """Update vault item properties."""
    update_data = update_in.model_dump(exclude_unset=True)
    for field, val in update_data.items():
        setattr(item, field, val)

    item.updated_at = datetime.datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item


async def delete_vault_item(session: AsyncSession, item: VaultItem) -> None:
    """Delete vault item."""
    await session.delete(item)
    await session.commit()


async def toggle_pin_item(session: AsyncSession, item: VaultItem) -> VaultItem:
    """Toggle pinned status."""
    item.is_pinned = not item.is_pinned
    item.updated_at = datetime.datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item


async def toggle_archive_item(session: AsyncSession, item: VaultItem) -> VaultItem:
    """Toggle archived status."""
    item.is_archived = not item.is_archived
    item.updated_at = datetime.datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item


async def increment_item_progress(session: AsyncSession, item: VaultItem, step: int = 1) -> VaultItem:
    """Quick increment episode/chapter progress."""
    item.progress_current = (item.progress_current or 0) + step
    if item.progress_total and item.progress_current >= item.progress_total:
        item.status = "completed"
    elif item.status in (None, "planned"):
        item.status = "watching"

    item.updated_at = datetime.datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item


async def list_vault_items(
    session: AsyncSession,
    q: str | None = None,
    entry_type: str | None = None,
    category: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    collection_id: int | None = None,
    parent_id: int | None = None,
    node_type: str | None = None,
    is_pinned: bool | None = None,
    is_archived: bool = False,
    sort_by: str = "created_at",
    sort_order: str = "desc",
    limit: int = 100,
    offset: int = 0,
) -> list[VaultItem]:
    """List vault items with dynamic filtering."""
    stmt = select(VaultItem)

    if is_archived is not None:
        stmt = stmt.where(VaultItem.is_archived == is_archived)

    if is_pinned is not None:
        stmt = stmt.where(VaultItem.is_pinned == is_pinned)

    if entry_type:
        stmt = stmt.where(VaultItem.entry_type == entry_type)

    if node_type:
        stmt = stmt.where(VaultItem.node_type == node_type)

    if category:
        stmt = stmt.where(VaultItem.category == category)

    if status:
        stmt = stmt.where(VaultItem.status == status)

    if collection_id is not None:
        stmt = stmt.where(VaultItem.collection_id == collection_id)

    if parent_id is not None:
        stmt = stmt.where(VaultItem.parent_id == parent_id)

    if q:
        query_str = f"%{q}%"
        stmt = stmt.where(
            (VaultItem.title.ilike(query_str))
            | (VaultItem.content.ilike(query_str))
            | (VaultItem.url.ilike(query_str))
            | (VaultItem.og_title.ilike(query_str))
        )

    # Sorting
    if sort_by == "score":
        order_col = VaultItem.score.desc() if sort_order == "desc" else VaultItem.score.asc()
    elif sort_by == "title":
        order_col = VaultItem.title.asc() if sort_order == "asc" else VaultItem.title.desc()
    elif sort_by == "updated_at":
        order_col = VaultItem.updated_at.desc() if sort_order == "desc" else VaultItem.updated_at.asc()
    else:
        # Default: Pinned items first, then created_at desc
        order_col = VaultItem.created_at.desc() if sort_order == "desc" else VaultItem.created_at.asc()

    stmt = stmt.order_by(VaultItem.is_pinned.desc(), order_col).offset(offset).limit(limit)

    res = await session.execute(stmt)
    items = list(res.scalars().all())

    # If tag filtering is specified in python (JSON array check)
    if tag:
        tag_lower = tag.lower()
        items = [it for it in items if it.tags and any(t.lower() == tag_lower for t in it.tags)]

    return items


async def get_vault_stats(session: AsyncSession) -> dict[str, Any]:
    """Calculate vault statistics summary."""
    stmt_all = select(VaultItem).where(not VaultItem.is_archived)
    res = await session.execute(stmt_all)
    items = list(res.scalars().all())

    total = len(items)
    bookmarks = sum(1 for i in items if i.entry_type == "bookmark")
    ratings = sum(1 for i in items if i.entry_type == "rating")
    thoughts = sum(1 for i in items if i.entry_type == "thought")
    completed = sum(1 for i in items if i.status == "completed")
    watching = sum(1 for i in items if i.status == "watching")
    pinned = sum(1 for i in items if i.is_pinned)

    # Archived count
    stmt_arch = select(func.count(VaultItem.id)).where(VaultItem.is_archived)
    arch_res = await session.execute(stmt_arch)
    archived_count = arch_res.scalar() or 0

    scores = [i.score for i in items if i.score is not None]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    # Categories breakdown
    categories_breakdown: dict[str, int] = {}
    for i in items:
        cat = i.category or "other"
        categories_breakdown[cat] = categories_breakdown.get(cat, 0) + 1

    # Tags frequency
    tag_counts: dict[str, int] = {}
    for i in items:
        if i.tags:
            for t in i.tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1

    sorted_tags = [
        {"tag": k, "count": v} for k, v in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    ]

    return {
        "total_items": total,
        "bookmarks_count": bookmarks,
        "ratings_count": ratings,
        "thoughts_count": thoughts,
        "completed_count": completed,
        "watching_count": watching,
        "pinned_count": pinned,
        "archived_count": archived_count,
        "avg_score": avg_score,
        "categories_breakdown": categories_breakdown,
        "top_tags": sorted_tags,
    }


async def create_collection(session: AsyncSession, coll_in: VaultCollectionCreate) -> VaultCollection:
    """Create a new collection folder."""
    coll = VaultCollection(
        name=coll_in.name,
        description=coll_in.description,
        color=coll_in.color or "teal",
        icon=coll_in.icon,
    )
    session.add(coll)
    await session.commit()
    await session.refresh(coll)
    return coll


async def list_collections(session: AsyncSession) -> list[VaultCollection]:
    """List all collections with items count."""
    stmt = select(VaultCollection).order_by(VaultCollection.name.asc())
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def delete_collection(session: AsyncSession, coll_id: int) -> None:
    """Delete a collection."""
    stmt = select(VaultCollection).where(VaultCollection.id == coll_id)
    res = await session.execute(stmt)
    coll = res.scalar_one_or_none()
    if coll:
        await session.delete(coll)
        await session.commit()


async def resolve_soft_entity_info(
    session: AsyncSession, entity_type: str, entity_id: str
) -> dict[str, Any] | None:
    """
    Soft integration helper:
    Safely query titles/thumbnails from other modules (Video Archiver, AllLib, Music, Torrent)
    WITHOUT hard dependency imports. If module is absent, fails silently.
    """
    if not entity_type or not entity_id:
        return None

    return await module_registry.resolve_entity(entity_type, entity_id, session)
