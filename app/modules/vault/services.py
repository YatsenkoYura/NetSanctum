import asyncio
import datetime
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import redis.asyncio as aioredis
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.modules import module_registry
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked, validate_remote_url
from app.modules.vault import images as _vault_images
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.schemas import (
    VaultCaptureCreate,
    VaultCollectionCreate,
    VaultItemCreate,
    VaultItemUpdate,
)
from app.modules.vault.tasks import download_vault_video_task

logger = logging.getLogger(__name__)

redis_client = aioredis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)

# Tracked download progress, declared by the module manifest.
MEDIA_PROGRESS_PREFIX = "vault_media"

# NOTE: embedded-image helpers are owned by images.py; these aliases keep existing
# `from app.modules.vault.services import decode_data_image` imports working.
decode_data_image = _vault_images.decode_data_image
LOCAL_IMAGE_PREFIXES = _vault_images.LOCAL_IMAGE_PREFIXES
LOCAL_IMAGE_MEDIA_TYPES = _vault_images.LOCAL_IMAGE_MEDIA_TYPES
# NOTE: MAX_IMAGE_BYTES lives in schemas.py; import it from there
# (`from app.modules.vault.schemas import MAX_IMAGE_BYTES`).


def vault_tag_filter(tag: str):
    """Build a case-insensitive exact match against a PostgreSQL JSON tag array."""
    tag_values = func.json_array_elements_text(VaultItem.tags).table_valued("value").alias("vault_tag")
    return exists(select(1).select_from(tag_values).where(func.lower(tag_values.c.value) == tag.lower()))


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


async def create_captured_item(
    session: AsyncSession,
    capture: VaultCaptureCreate,
    *,
    user: Any = None,
) -> VaultItem:
    """Store an extension capture exactly like a dashboard paste or bookmark.

    The image travels as a data URL and lands in `og_image`, so the item reuses
    the existing embedded-image path: the list endpoint omits the bytes and
    `/api/vault/items/{id}/image` serves them. Metadata fetching is off unless
    the caller asks for it, because the extension already knows the title.
    """
    if capture.kind == "video":
        return await create_video_capture_item(session, capture, user=user)
    if not decode_data_image(capture.image or ""):
        raise ValueError("Capture image must be a supported data:image URL within the size limit")

    content_parts = []
    if capture.alt_text:
        content_parts.append(capture.alt_text)
    if capture.source_url and capture.source_url != capture.page_url:
        content_parts.append(f"Source: {capture.source_url}")
    if capture.page_url:
        content_parts.append(f"Page: {capture.page_url}")

    return await create_vault_item(
        session,
        VaultItemCreate(
            entry_type="bookmark",
            node_type="image",
            title=capture.title,
            content="\n\n".join(content_parts) or None,
            url=capture.page_url,
            og_title=capture.title,
            og_description=capture.alt_text,
            og_image=capture.image,
            tags=capture.tags,
            collection_id=capture.collection_id,
            parent_id=capture.parent_id,
            auto_fetch_og=capture.auto_fetch_og,
        ),
    )


def _is_acceptable_still(image: str | None) -> bool:
    """A video's preview may be an address, not just embedded bytes.

    A poster is normally a remote URL, and Vault already serves those through
    its preview proxy. Embedded bytes still have to decode.
    """
    if not image:
        return True
    if image.startswith("data:"):
        return bool(decode_data_image(image))
    parsed = urlparse(image)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


async def queue_video_download(
    session: AsyncSession,
    item_id: int,
    url: str,
    *,
    quality: str = "720",
    title: str | None = None,
) -> str | None:
    """Ask the worker to pull the video into Vault's storage.

    Dispatch failures are not fatal: the card is already written and simply keeps
    its "not downloaded" state, which the card renders honestly instead of
    pretending the video is there.
    """
    try:
        from app.core.task_dispatch import dispatch_tracked_async

        task = await dispatch_tracked_async(
            download_vault_video_task,
            redis_client,
            MEDIA_PROGRESS_PREFIX,
            {"url": url, "item_id": item_id, "status": "queued", "progress": "0%"},
            kwargs={"item_id": item_id, "url": url, "quality": quality, "title": title},
        )
    except TypeError:
        # The task is not a task. That is a defect in this file, not an
        # unavailable worker, and the card cannot ever download until it is fixed —
        # so it must not be logged as a transient warning.
        logger.error("the Vault video task is not a registered Celery task", exc_info=True)
        return None
    except Exception:
        logger.warning("could not queue a Vault video download for item %s", item_id, exc_info=True)
        return None
    return task.id


async def create_video_capture_item(
    session: AsyncSession,
    capture: VaultCaptureCreate,
    *,
    user: Any = None,
) -> VaultItem:
    """Record a video capture in Vault and queue the download into Vault's own storage.

    Vault keeps the file itself. Pushing it at another module by default made the
    two records drift apart — the archive built its own title and thumbnail while
    Vault held a bare note pointing at a job id that expires with Redis — and it
    meant picking a video could silently fail for reasons the user never chose.
    Handing a stored video to another module is a separate, explicit action.
    """
    if capture.image and not _is_acceptable_still(capture.image):
        raise ValueError("Capture image must be a supported data:image URL or an HTTP image address")

    parts = []
    if capture.source_url and capture.source_url != capture.page_url:
        # Shown as text on purpose: a blob: or data: provenance string is not an
        # address, and must never be promoted to the item's clickable link.
        parts.append(f"Source: {capture.source_url}")

    item = await create_vault_item(
        session,
        VaultItemCreate(
            entry_type="bookmark",
            node_type="video",
            title=capture.title,
            content="\n\n".join(parts) or None,
            url=capture.page_url or capture.video_url,
            og_title=capture.title,
            og_description=capture.alt_text,
            og_image=capture.image,
            tags=capture.tags,
            collection_id=capture.collection_id,
            parent_id=capture.parent_id,
            auto_fetch_og=capture.auto_fetch_og,
        ),
    )
    task_id = await queue_video_download(
        session,
        item.id,
        str(capture.video_url),
        quality=capture.quality,
        title=capture.title,
    )
    # The job id lives in the card's own metadata, not in related_entity_id:
    # it is a Redis key with a 24h life, and a durable field holding an expired
    # id is exactly the dead link this flow used to have.
    item.canvas_data = {
        **(item.canvas_data or {}),
        "media_status": "queued" if task_id else "not queued",
        "media_task": task_id,
    }
    await session.commit()
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


def is_readable_item():
    """SQL predicate for "this item may appear on a board that needs no passphrase".

    Both halves matter: `sealed_payload` catches an item that was sealed, and the
    subquery catches one that was inserted into a sealed collection before it was
    sealed. An empty filter would leak the board; a half-filter would leak a row.
    """
    sealed_collections = select(VaultCollection.id).where(VaultCollection.is_encrypted.is_(True))
    return and_(
        VaultItem.sealed_payload.is_(None),
        or_(VaultItem.collection_id.is_(None), ~VaultItem.collection_id.in_(sealed_collections)),
    )


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
    else:
        # The aggregate view ("Все карточки") must not surface a sealed Vault.
        # A card cannot be opened without the passphrase, but its existence, its
        # alias and its rating are still information the owner chose to keep out
        # of the shared board.
        stmt = stmt.where(is_readable_item())

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

    if tag:
        stmt = stmt.where(vault_tag_filter(tag))

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

    stmt = stmt.order_by(VaultItem.is_pinned.desc(), order_col)

    res = await session.execute(stmt.offset(offset).limit(limit))
    return list(res.scalars().all())


async def list_vault_package_items(session: AsyncSession) -> list[VaultItem]:
    """Return the complete, deterministically ordered offline Vault snapshot.

    Sealed items are excluded. A package is a portable copy handed to someone else,
    so including a sealed row would export ciphertext nobody can read and its alias
    to everybody.
    """
    stmt = (
        select(VaultItem)
        .where(VaultItem.is_archived.is_(False), is_readable_item())
        .order_by(VaultItem.is_pinned.desc(), VaultItem.created_at.desc(), VaultItem.id.desc())
    )
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_vault_stats(session: AsyncSession) -> dict[str, Any]:
    """Calculate vault statistics summary with SQL aggregates (no full-row load)."""
    # Sealed items are excluded here too: the sidebar counted them, which leaked
    # how many private records exist and how they were rated.
    active = and_(VaultItem.is_archived.is_(False), is_readable_item())

    async def count_where(*conditions) -> int:
        result = await session.execute(select(func.count(VaultItem.id)).where(*conditions))
        return result.scalar() or 0

    total = await count_where(active)
    bookmarks = await count_where(active, VaultItem.entry_type == "bookmark")
    ratings = await count_where(active, VaultItem.entry_type == "rating")
    thoughts = await count_where(active, VaultItem.entry_type == "thought")
    completed = await count_where(active, VaultItem.status == "completed")
    watching = await count_where(active, VaultItem.status == "watching")
    pinned = await count_where(active, VaultItem.is_pinned.is_(True))
    archived_count = await count_where(VaultItem.is_archived.is_(True), is_readable_item())

    avg_result = await session.execute(
        select(func.avg(VaultItem.score)).where(active, VaultItem.score.is_not(None))
    )
    avg_value = avg_result.scalar()
    avg_score = round(float(avg_value), 1) if avg_value is not None else 0.0

    # Categories breakdown (group by the bare column: repeating a coalesce()
    # in SELECT and GROUP BY yields distinct bind params, which PostgreSQL
    # rejects with a GroupingError).
    categories_breakdown: dict[str, int] = {}
    cat_rows = await session.execute(
        select(VaultItem.category, func.count(VaultItem.id)).where(active).group_by(VaultItem.category)
    )
    for category, count in cat_rows.all():
        categories_breakdown[category or "other"] = count

    # Tags frequency (tags column only — full rows stay out of memory)
    tag_counts: dict[str, int] = {}
    tag_rows = await session.execute(select(VaultItem.tags).where(active))
    for (tags,) in tag_rows.all():
        if tags:
            for tag in tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

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
    stmt = (
        select(VaultCollection, func.count(VaultItem.id))
        .outerjoin(
            VaultItem,
            (VaultItem.collection_id == VaultCollection.id) & (VaultItem.is_archived.is_(False)),
        )
        .group_by(VaultCollection.id)
        .order_by(VaultCollection.name.asc())
    )
    res = await session.execute(stmt)
    collections = []
    for collection, items_count in res.all():
        collection.items_count = items_count
        collections.append(collection)
    return collections


async def delete_collection(session: AsyncSession, coll_id: int) -> None:
    """Delete a collection."""
    stmt = select(VaultCollection).where(VaultCollection.id == coll_id)
    res = await session.execute(stmt)
    coll = res.scalar_one_or_none()
    if coll:
        await session.delete(coll)
        await session.commit()


class VaultCollectionNotFoundError(LookupError):
    """The merge source or target workspace does not exist."""


class VaultMergeError(ValueError):
    """The merge would break the sealed-workspace promise."""


async def merge_collections(session: AsyncSession, from_id: int, into_id: int | None) -> int:
    """Move every card from one workspace into another and delete the emptied one.

    Cards keep their ids, history and data — only the workspace link changes, so
    the target reads as one space with the merged cards at its end. Sealed
    workspaces are refused outright: a sealed card moved under a plain
    collection would lose the key lookup that opens it and stay locked forever.
    """
    if into_id is not None and from_id == into_id:
        raise VaultMergeError("Нельзя слить воркспейс с самим собой")
    src = await session.get(VaultCollection, from_id)
    if src is None:
        raise VaultCollectionNotFoundError(f"Workspace {from_id} not found")
    dst = None
    if into_id is not None:
        dst = await session.get(VaultCollection, into_id)
        if dst is None:
            raise VaultCollectionNotFoundError(f"Workspace {into_id} not found")
    if src.is_encrypted or (dst is not None and dst.is_encrypted):
        raise VaultMergeError("Зашифрованные воркспейсы нельзя сливать")
    result = await session.execute(select(VaultItem).where(VaultItem.collection_id == from_id))
    items = list(result.scalars().all())
    for item in items:
        item.collection_id = into_id
    await session.delete(src)
    await session.commit()
    return len(items)


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
