import asyncio
import datetime
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import redis.asyncio as aioredis
from sqlalchemy import and_, exists, func, or_, select, update
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
from app.modules.vault.sealing import VaultMoveError
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
    await session.flush()
    await place_new_card(session, item)
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
    # Structural column, like `media_path`: `canvas_data` is a sealed field, so a
    # write there was silently dropped for a locked collection and visible in the
    # clear before that. The task id used to sit here too — write-only, read by
    # nothing, and it expired within a day anyway.
    item.media_status = "queued" if task_id else "not queued"
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
    if "collection_id" in update_data:
        await _assert_can_move_card(session, item, update_data["collection_id"])
    for field, val in update_data.items():
        setattr(item, field, val)

    item.updated_at = datetime.datetime.utcnow()
    await session.commit()
    await session.refresh(item)
    return item


async def _assert_can_move_card(session: AsyncSession, item: VaultItem, collection_id: int | None) -> None:
    """Refuse a card move that would leave it readable-looking but unopenable.

    A sealed card carries its own wrapped key, and a plain card inside a sealed
    space is filtered out of every read path. Both are one-way mistakes: the card
    either disappears from every list, or stays a locked tile with no way to open
    it, because its key lookup is gone. Merging refuses the same moves for the
    same reason; this is the same guard on the path a drag takes.
    """
    if collection_id == item.collection_id:
        return
    if item.sealed_payload:
        raise VaultMoveError("Зашифрованную карточку нельзя перенести в другое пространство")
    if collection_id is not None:
        target = await session.get(VaultCollection, collection_id)
        if target is not None and target.is_encrypted:
            raise VaultMoveError("В зашифрованное пространство можно переносить только закрытые карточки")


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


def _ordered(column, sort_order: str):
    """A sort column in the requested direction."""
    return column.desc() if sort_order == "desc" else column.asc()


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
    sort_by: str = "manual",
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

    # Sorting. The default is the owner's own arrangement: positions are assigned
    # on creation and rewritten by every drag, so nothing is decided by when a
    # thing happened to be saved any more. `nullslast` covers rows written by
    # something that went through neither path.
    if sort_by == "score":
        order_by = [VaultItem.is_pinned.desc(), _ordered(VaultItem.score, sort_order)]
    elif sort_by == "title":
        order_by = [VaultItem.is_pinned.desc(), _ordered(VaultItem.title, sort_order)]
    elif sort_by == "updated_at":
        order_by = [VaultItem.is_pinned.desc(), _ordered(VaultItem.updated_at, sort_order)]
    elif sort_by == "created_at":
        order_by = [VaultItem.is_pinned.desc(), _ordered(VaultItem.created_at, sort_order)]
    else:
        order_by = [
            VaultItem.is_pinned.desc(),
            VaultItem.position.asc().nullslast(),
            VaultItem.created_at.desc(),
        ]
    # The id tiebreaker is what makes a page boundary stable: two cards sharing a
    # sort value must not swap places between requests.
    order_by.append(VaultItem.id.asc())

    stmt = stmt.order_by(*order_by)
    res = await session.execute(stmt.offset(offset).limit(limit))
    return list(res.scalars().all())


async def list_vault_package_items(session: AsyncSession) -> list[VaultItem]:
    """Return the complete, deterministically ordered offline Vault snapshot.

    Sealed items are excluded. A package is a portable copy handed to someone else,
    so including a sealed row would export ciphertext nobody can read and its alias
    to everybody. The owner's own order travels with it, so an exported space
    reads the way it did on the instance.
    """
    stmt = (
        select(VaultItem)
        .where(VaultItem.is_archived.is_(False), is_readable_item())
        .order_by(
            VaultItem.is_pinned.desc(),
            VaultItem.position.asc().nullslast(),
            VaultItem.created_at.desc(),
            VaultItem.id.asc(),
        )
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
        parent_id=coll_in.parent_id,
    )
    session.add(coll)
    await session.flush()
    # A new space lands at the end of its level. Cards go to the front because
    # that is where a new card used to appear; a new folder at the top of the
    # sidebar would just push everything down.
    await place_new_space(session, coll)
    await session.commit()
    await session.refresh(coll)
    return coll


async def list_collections(session: AsyncSession) -> list[VaultCollection]:
    """List all collections with items count, in sibling order."""
    stmt = (
        select(VaultCollection, func.count(VaultItem.id))
        .outerjoin(
            VaultItem,
            (VaultItem.collection_id == VaultCollection.id) & (VaultItem.is_archived.is_(False)),
        )
        .group_by(VaultCollection.id)
        .order_by(VaultCollection.position.asc().nullslast(), VaultCollection.name.asc())
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


# ── Space tree and manual order ────────────────────────────────────────────
# A space can hold other spaces, and a card remembers where its owner put it.
# Both use fractional positions: a drop between two neighbours writes the
# midpoint, so ordering costs one row instead of renumbering the level. When two
# neighbours get squeezed together until there is no room left between them, the
# level is renumbered — the only case where a drag touches more than one row.

POSITION_STEP = 1024.0
# Below this there is no representable midpoint left between two neighbours.
POSITION_MIN_GAP = 1e-6


class VaultOrderError(ValueError):
    """The requested drop does not make sense where it was asked for."""


def _midpoint(before: float | None, after: float | None) -> float:
    """A position strictly between two neighbours, or outside them if one is missing."""
    if before is None and after is None:
        return 0.0
    if before is None:
        return float(after) - POSITION_STEP
    if after is None:
        return float(before) + POSITION_STEP
    return (float(before) + float(after)) / 2


async def _neighbour_positions(
    session: AsyncSession,
    collection_id: int | None,
    before_id: int | None,
    after_id: int | None,
) -> tuple[float | None, float | None]:
    """The positions of the two cards a card was dropped between."""
    wanted = [row_id for row_id in (before_id, after_id) if row_id is not None]
    found: dict[int, float | None] = {}
    if wanted:
        rows = await session.execute(
            select(VaultItem.id, VaultItem.position, VaultItem.collection_id).where(VaultItem.id.in_(wanted))
        )
        for row_id, position, row_collection in rows.all():
            if row_collection != collection_id:
                raise VaultOrderError("Карточка и её сосед лежат в разных пространствах")
            found[row_id] = position
        missing = set(wanted) - set(found)
        if missing:
            raise VaultOrderError("Соседняя карточка не найдена")
    return found.get(before_id), found.get(after_id)


async def _renumber(session: AsyncSession, collection_id: int | None) -> None:
    """Give the space fresh, evenly spaced positions."""
    rows = await session.execute(
        select(VaultItem.id, VaultItem.created_at, VaultItem.id)
        .where(VaultItem.collection_id == collection_id)
        .order_by(VaultItem.created_at.asc(), VaultItem.id.asc())
    )
    for index, (item_id, _created, _id) in enumerate(rows.all()):
        await session.execute(
            update(VaultItem).where(VaultItem.id == item_id).values(position=float(index) * POSITION_STEP)
        )


async def reorder_card(
    session: AsyncSession,
    item_id: int,
    before_id: int | None,
    after_id: int | None,
) -> VaultItem:
    """Move one card to where it was dropped, between two of its neighbours.

    The neighbours are ids, not positions: the client says "between these two
    cards", and the server works out the number. That keeps a drop meaningful even
    if two tabs disagree about the current order.
    """
    item = await session.get(VaultItem, item_id)
    if item is None:
        raise VaultOrderError("Карточка не найдена")
    if before_id is not None and after_id is not None and before_id == after_id:
        raise VaultOrderError("Нельзя вставить карточку саму в себя")
    if item_id in (before_id, after_id):
        raise VaultOrderError("Нельзя вставить карточку саму в себя")

    before, after = await _neighbour_positions(session, item.collection_id, before_id, after_id)
    position = _midpoint(before, after)
    if before is not None and after is not None and abs(float(after) - float(before)) < POSITION_MIN_GAP:
        # The neighbours have been squeezed together; nothing fits between them
        # any more, so the level gets fresh numbers and the card is placed again.
        await _renumber(session, item.collection_id)
        before, after = await _neighbour_positions(session, item.collection_id, before_id, after_id)
        position = _midpoint(before, after)

    item.position = position
    await session.commit()
    await session.refresh(item)
    return item


async def place_new_card(session: AsyncSession, item: VaultItem) -> None:
    """Put a freshly created card at the front of its space.

    The grid used to order by `created_at desc`, so a new card appeared first.
    Preserving that means a new card takes a slot before the current first one
    rather than being appended to the end.
    """
    result = await session.execute(
        select(func.min(VaultItem.position)).where(VaultItem.collection_id == item.collection_id)
    )
    current_min = result.scalar()
    item.position = (float(current_min) if current_min is not None else 0.0) - POSITION_STEP


async def reorder_space(
    session: AsyncSession,
    collection_id: int,
    parent_id: int | None,
    before_id: int | None = None,
    after_id: int | None = None,
) -> VaultCollection:
    """Nest a space under another one, or move it among its siblings."""
    collection = await session.get(VaultCollection, collection_id)
    if collection is None:
        raise VaultCollectionNotFoundError(f"Пространство {collection_id} не найдено")
    await _assert_can_nest(session, collection, parent_id)

    siblings_before, siblings_after = await _space_neighbour_positions(
        session, parent_id, before_id, after_id, moving=collection
    )
    collection.parent_id = parent_id
    collection.position = _midpoint(siblings_before, siblings_after)
    await session.commit()
    await session.refresh(collection)
    return collection


async def _space_neighbour_positions(
    session: AsyncSession,
    parent_id: int | None,
    before_id: int | None,
    after_id: int | None,
    *,
    moving: VaultCollection | None = None,
) -> tuple[float | None, float | None]:
    """Positions of the sibling spaces a space was dropped between."""
    wanted = [row_id for row_id in (before_id, after_id) if row_id is not None]
    found: dict[int, float | None] = {}
    if wanted:
        rows = await session.execute(
            select(VaultCollection.id, VaultCollection.position, VaultCollection.parent_id).where(
                VaultCollection.id.in_(wanted)
            )
        )
        for row_id, position, row_parent in rows.all():
            if moving is not None and row_id == moving.id:
                continue
            if row_parent != parent_id:
                raise VaultOrderError("Соседнее пространство лежит в другой ветке")
            found[row_id] = position
        missing = set(wanted) - set(found)
        if missing:
            raise VaultOrderError("Соседнее пространство не найдено")
    return found.get(before_id), found.get(after_id)


async def _ancestor_ids(session: AsyncSession, collection_id: int) -> set[int]:
    """Every space above `collection_id`, so a drop cannot build a cycle."""
    ancestors: set[int] = set()
    current: int | None = collection_id
    while current is not None:
        row = await session.get(VaultCollection, current)
        if row is None or row.parent_id is None:
            break
        if row.parent_id in ancestors:
            break
        ancestors.add(row.parent_id)
        current = row.parent_id
    return ancestors


async def _assert_can_nest(session: AsyncSession, collection: VaultCollection, parent_id: int | None) -> None:
    """Refuse any nesting that would leak a sealed space or build a loop."""
    if collection.is_encrypted and parent_id is not None:
        raise VaultOrderError("Зашифрованное пространство всегда остаётся на верхнем уровне")
    if parent_id is None:
        return
    if parent_id == collection.id:
        raise VaultOrderError("Пространство нельзя вложить в само себя")
    parent = await session.get(VaultCollection, parent_id)
    if parent is None:
        raise VaultCollectionNotFoundError(f"Пространство {parent_id} не найдено")
    if parent.is_encrypted:
        # A locked sealed space shows only its alias, so children hanging under it
        # would either be invisible or would have to show their names next to it.
        raise VaultOrderError("В зашифрованное пространство нельзя вложить другое")
    # The loop to look for is above the *target*: if the space being moved is
    # among the target's ancestors, the target is its own descendant.
    if collection.id in await _ancestor_ids(session, parent_id):
        raise VaultOrderError("Такой перенос сделает пространство собственным потомком")


async def list_child_spaces(session: AsyncSession, collection_id: int | None) -> list[VaultCollection]:
    """The spaces nested under one, in their saved order.

    The root level is spelled `IS NULL` rather than a bound parameter: Postgres
    rejects `parent_id IS $1`, while SQLite accepts it without complaint.
    """
    parent_clause = (
        VaultCollection.parent_id.is_(None)
        if collection_id is None
        else VaultCollection.parent_id == collection_id
    )
    stmt = (
        select(VaultCollection)
        .where(parent_clause)
        .order_by(VaultCollection.position.asc().nullslast(), VaultCollection.name.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def place_new_space(session: AsyncSession, collection: VaultCollection) -> None:
    """Put a freshly created space at the end of its level."""
    result = await session.execute(
        select(func.max(VaultCollection.position)).where(VaultCollection.parent_id == collection.parent_id)
    )
    current_max = result.scalar()
    collection.position = (float(current_max) if current_max is not None else -POSITION_STEP) + POSITION_STEP


class VaultDissolveError(ValueError):
    """The folder cannot be dissolved where it stands."""


async def _renumber_spaces(session: AsyncSession, parent_id: int | None) -> None:
    """Give one level of spaces fresh, evenly spaced positions."""
    rows = await session.execute(
        select(VaultCollection.id)
        .where(
            VaultCollection.parent_id.is_(None)
            if parent_id is None
            else VaultCollection.parent_id == parent_id
        )
        .order_by(VaultCollection.position.asc().nullslast(), VaultCollection.name.asc())
    )
    for index, (collection_id,) in enumerate(rows.all()):
        await session.execute(
            update(VaultCollection)
            .where(VaultCollection.id == collection_id)
            .values(position=float(index) * POSITION_STEP)
        )


async def dissolve_space(session: AsyncSession, collection_id: int) -> dict[str, int]:
    """Dissolve a folder: its contents move up and take the folder's own place.

    A folder is a space with a parent. Dissolving it lifts both what hangs under
    it (nested spaces) and what lives in it (cards) into the parent, and the
    lifted spaces land where the folder was in the parent's order rather than at
    the end of it — losing that place would quietly reorder somebody's sidebar.

    Their relative order is kept: the folder's children come out in the order
    they were in, and the level is renumbered afterwards because positions are
    no longer meaningful once a slot is gone.

    Refused for a sealed space (it has no parent by construction) and for a root
    space (there is nothing to dissolve it into).
    """
    folder = await session.get(VaultCollection, collection_id)
    if folder is None:
        raise VaultCollectionNotFoundError(f"Пространство {collection_id} не найдено")
    if folder.is_encrypted:
        raise VaultDissolveError("Зашифрованное пространство нельзя распустить")
    if folder.parent_id is None:
        raise VaultDissolveError("Это пространство не вложено и распускать нечего")

    parent_id = folder.parent_id
    parent = await session.get(VaultCollection, parent_id)
    if parent is None:
        raise VaultCollectionNotFoundError(f"Пространство {parent_id} не найдено")
    if parent.is_encrypted:
        raise VaultDissolveError("В зашифрованное пространство нельзя ничего распустить")

    # Where the folder sat among its siblings, and who came after it. The lifted
    # spaces are spliced in at that index, which is what "takes the folder's
    # place" means once the positions are renumbered.
    siblings = list(
        (
            await session.execute(
                select(VaultCollection.id, VaultCollection.position)
                .where(VaultCollection.parent_id == parent_id)
                .order_by(VaultCollection.position.asc().nullslast(), VaultCollection.name.asc())
            )
        ).all()
    )
    folder_index = next((i for i, (sid, _pos) in enumerate(siblings) if sid == collection_id), len(siblings))

    inner = list(
        (
            await session.execute(
                select(VaultCollection.id)
                .where(VaultCollection.parent_id == collection_id)
                .order_by(VaultCollection.position.asc().nullslast(), VaultCollection.name.asc())
            )
        ).all()
    )
    for (space_id,) in inner:
        space = await session.get(VaultCollection, space_id)
        space.parent_id = parent_id

    cards = list(
        (await session.execute(select(VaultItem).where(VaultItem.collection_id == collection_id))).scalars()
    )
    for card in cards:
        card.collection_id = parent_id

    await session.delete(folder)
    await session.flush()

    order = [sid for sid, _pos in siblings if sid != collection_id]
    order[folder_index:folder_index] = [sid for (sid,) in inner]
    for position, space_id in enumerate(order):
        await session.execute(
            update(VaultCollection)
            .where(VaultCollection.id == space_id)
            .values(position=float(position) * POSITION_STEP)
        )
    await session.commit()

    return {"spaces": len(inner), "cards": len(cards), "index": folder_index}
