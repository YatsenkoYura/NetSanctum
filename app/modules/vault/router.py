import asyncio
import random

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.module_types import (
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
)
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked
from app.core.security import get_current_bearer_user, get_current_user
from app.core.storage import get_storage
from app.core.templates import templates
from app.modules.vault.capabilities import VAULT_PACKAGE_ID
from app.modules.vault.crypto import VaultUnlockError
from app.modules.vault.images import (
    LOCAL_IMAGE_PREFIXES,
    decode_data_image,
    externalize_image,
    has_image,
    image_bytes,
    media_type_for,
)
from app.modules.vault.models import VaultCollection
from app.modules.vault.schemas import (
    VaultCaptureCreate,
    VaultCaptureResponse,
    VaultCollectionCreate,
    VaultCollectionMerge,
    VaultCollectionMove,
    VaultCollectionResponse,
    VaultItemCreate,
    VaultItemMove,
    VaultItemResponse,
    VaultItemUpdate,
    VaultLockResponse,
    VaultStatsResponse,
    VaultUnlockRequest,
    VaultUnlockResponse,
)
from app.modules.vault.sealing import (
    DEFAULT_ITEM_ALIAS,
    DEFAULT_SEALED_ALIAS,
    SEALED_FIELDS,
    collection_for,
    create_sealed_collection,
    data_key_for,
    is_sealed_collection,
    lock_collection,
    locked_collection_ids,
    open_item,
    open_items,
    require_inbox_public_key,
    seal_item,
    sealed_collection_ids,
    unlock_collection,
    update_sealed_item,
)
from app.modules.vault.services import (
    VaultCollectionNotFoundError,
    VaultDissolveError,
    VaultMergeError,
    VaultMoveError,
    VaultOrderError,
    create_captured_item,
    create_collection,
    create_vault_item,
    delete_collection,
    delete_vault_item,
    dissolve_space,
    fetch_url_metadata,
    get_vault_item,
    get_vault_stats,
    increment_item_progress,
    list_child_spaces,
    list_collections,
    list_vault_items,
    list_vault_package_items,
    merge_collections,
    reorder_card,
    reorder_space,
    resolve_soft_entity_info,
    toggle_archive_item,
    toggle_pin_item,
    update_vault_item,
)

router = APIRouter()

# The unlock token is per-tab: it exists only in one page's memory, so a reload
# loses it and the passphrase is required again. Anything that reads a sealed
# Vault takes it as a header rather than reading a server-side session.
UNLOCK_HEADER = Header(None, alias="X-Vault-Unlock")
settings = get_settings()


async def _get_lang(request: Request) -> str:
    """Resolve active language cookie."""
    return request.cookies.get("lang", "ru")


# ── UI Pages ─────────────────────────────────────────────


@router.get("/vault/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def vault_dashboard(
    request: Request,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    unlock_token: str = UNLOCK_HEADER,
):
    """Serve the primary Vault personal scrapbook & tracker dashboard."""
    lang = await _get_lang(request)
    collections = await list_collections(db)
    stats = await get_vault_stats(db)
    # Serialized, not raw rows: the sidebar is rendered on the server, and a raw
    # VaultCollection would put a locked vault's real name into the first HTML
    # response before any of the lock-aware JavaScript runs.
    locked = await locked_collection_ids(db, unlock_token)
    serializable = [
        _serialize_collection(collection, locked=collection.id in locked) for collection in collections
    ]

    return templates.TemplateResponse(
        request,
        "vault_dashboard.html",
        {
            "user": user,
            "lang": lang,
            "collections": serializable,
            "stats": stats,
        },
    )


# ── REST API Endpoints ────────────────────────────────────


@router.get("/api/vault/items", response_model=list[VaultItemResponse])
async def get_items(
    q: str | None = Query(None),
    entry_type: str | None = Query(None),
    category: str | None = Query(None),
    status: str | None = Query(None),
    tag: str | None = Query(None),
    collection_id: int | None = Query(None),
    parent_id: int | None = Query(None),
    node_type: str | None = Query(None),
    is_pinned: bool | None = Query(None),
    is_archived: bool = Query(False),
    sort_by: str = Query("manual"),
    sort_order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    package_id: str | None = Query(None),
    include_images: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """List vault items with dynamic filter parameters."""
    if package_id and package_id != VAULT_PACKAGE_ID:
        raise HTTPException(status_code=400, detail="Invalid Vault package ID")
    items = await list_vault_items(
        session=db,
        q=q,
        entry_type=entry_type,
        category=category,
        status=status,
        tag=tag,
        collection_id=collection_id,
        parent_id=parent_id,
        node_type=node_type,
        is_pinned=is_pinned,
        is_archived=is_archived,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
    )
    if package_id:
        return [_serialize_package_item(item) for item in items]

    # Sealed collections are opened here, in the one place that reads a whole page
    # of items, so no other endpoint has to remember to do it.
    locked = await locked_collection_ids(db, unlock_token)
    await open_items(db, items, unlock_token)
    serialized = [
        _apply_lock_state(
            # Always a dict: handing this an ORM row would write the alias into a
            # column the next commit flushes.
            _serialize_full_item(item) if include_images else _serialize_list_item(item),
            item,
            locked=item.collection_id in locked,
        )
        for item in items
    ]
    return serialized


def _serialize_list_item(item) -> dict:
    """Light list serialization: embedded images stay out of the payload.

    A pasted photo can be several megabytes of base64; inlining hundreds of
    them made the list endpoint take tens of seconds. Tiles load the bytes
    lazily via `/api/vault/items/{id}/image` when `has_image` is set.
    """
    serialized = VaultItemResponse.model_validate(item).model_dump()
    if decode_data_image(serialized.get("og_image")):
        serialized["og_image"] = None
    # `has_image` covers both carriers: the file and the legacy data URL.
    serialized["has_image"] = has_image(item)
    _apply_media_state(serialized, item)
    return serialized


def _serialize_full_item(item) -> dict:
    """Every field, including the embedded image bytes the list endpoint drops."""
    serialized = VaultItemResponse.model_validate(item).model_dump()
    _apply_media_state(serialized, item)
    return serialized


def _apply_lock_state(serialized: dict, item, *, locked: bool) -> dict:
    """Swap in the alias when a sealed item is locked, and say so.

    This is the boundary that keeps a locked vault from leaking through the list
    endpoint: the readable columns are already blank on disk, so the only way a
    real title could appear here is if it were decrypted first.
    """
    sealed = bool(getattr(item, "sealed_payload", None))
    alias = getattr(item, "public_title", None) or DEFAULT_ITEM_ALIAS
    serialized["is_sealed"] = sealed
    serialized["is_locked"] = bool(sealed and locked)
    serialized["public_title"] = alias if sealed else None
    if sealed and locked:
        serialized["title"] = alias
        # Driven off SEALED_FIELDS rather than a hand-written list: a field added
        # to the sealed set must not silently start leaking here.
        for field in SEALED_FIELDS:
            serialized[field] = None
        serialized["title"] = alias
        serialized["tags"] = []
        serialized["canvas_data"] = {}
        serialized["has_image"] = False
        serialized["has_media"] = False
        serialized["media_status"] = "locked"
        serialized["media_duration"] = None
    return serialized


def _apply_media_state(serialized: dict, item) -> dict:
    """Expose what a media card needs without leaking its storage path."""
    serialized["has_media"] = bool(getattr(item, "media_path", None))
    serialized["media_status"] = getattr(item, "media_status", None)
    duration = getattr(item, "media_duration", None)
    serialized["media_duration"] = int(duration) if isinstance(duration, (int, float)) else None
    # A download that already produced a poster should use it as the card face.
    if serialized["has_media"] and not serialized.get("has_image") and item.media_thumbnail_path:
        serialized["og_image"] = f"/api/vault/items/{item.id}/thumbnail"
    return serialized


def _serialize_package_item(item) -> dict:
    serialized = VaultItemResponse.model_validate(item).model_dump()
    image = serialized.get("og_image")
    if image and not image.lower().startswith(LOCAL_IMAGE_PREFIXES):
        serialized["og_image"] = None
    return serialized


@router.get("/api/vault/package-items", response_model=list[VaultItemResponse])
async def get_package_items(
    package_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Return one complete and deterministic item snapshot for the Vault package."""
    if package_id != VAULT_PACKAGE_ID:
        raise HTTPException(status_code=400, detail="Invalid Vault package ID")

    items = await list_vault_package_items(db)
    return [_serialize_package_item(item) for item in items]


@router.post("/api/vault/items", response_model=VaultItemResponse)
async def create_item(
    item_in: VaultItemCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Create a new vault item (bookmark, rating, thought).

    A sealed collection accepts this without the passphrase: the write is sealed
    under its public inbox key, which is exactly the blind write the extension
    relies on. Nothing readable is stored, locked or not.
    """
    item = await create_vault_item(db, item_in)
    if externalize_image(item):
        await db.commit()
        await db.refresh(item)
    collection = await collection_for(db, item.collection_id)
    locked = False
    if is_sealed_collection(collection):
        item.public_title = item_in.public_title or DEFAULT_ITEM_ALIAS
        seal_item(item, require_inbox_public_key(collection))
        await db.commit()
        await db.refresh(item)
        locked = await data_key_for(collection, unlock_token) is None
    return _apply_lock_state(_serialize_full_item(item), item, locked=locked)


@router.post("/api/vault/capture", response_model=VaultCaptureResponse, status_code=201)
async def create_capture(
    capture_in: VaultCaptureCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_bearer_user),
):
    """Store a screenshot, a media element, or a queued video from the extension.

    The endpoint takes a bearer token only: an extension is an external client,
    and letting it ride the browser session cookie would hand the owner's
    session to any page that can reach this path.
    """
    try:
        item = await create_captured_item(db, capture_in, user=user)
        externalize_image(item)
        # A capture into a sealed collection must be sealed too. This used to be
        # missing entirely, which meant the extension wrote screenshots, titles and
        # URLs into a locked vault in the clear, silently.
        collection = await collection_for(db, getattr(item, "collection_id", None))
        if is_sealed_collection(collection):
            item.public_title = capture_in.public_title or DEFAULT_ITEM_ALIAS
            seal_item(item, require_inbox_public_key(collection))
            await db.commit()
            await db.refresh(item)
    except IntegrationUnavailableError as exc:
        # The module that owns video archiving is not installed or not enabled.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except IntegrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntegrationRejectedError as exc:
        # The archive refused the URL: unsupported platform, or a playlist.
        # It is a ValueError subclass, so it must be caught before the generic
        # validation failure below.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    messages = {
        "screenshot": "Screenshot saved to Vault",
        "media": "Media saved to Vault",
        "video": "Video queued for archiving",
    }
    return VaultCaptureResponse(
        item_id=item.id,
        kind=capture_in.kind,
        title=item.title,
        image_url=f"/api/vault/items/{item.id}/image" if has_image(item) else None,
        task_id=item.related_entity_id if capture_in.kind == "video" else None,
        message=messages[capture_in.kind],
    )


@router.get("/api/vault/items/{item_id}/media", include_in_schema=False)
async def get_item_media(
    item_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Stream the video a media card owns, so the file lives with the entry.

    Range requests are honoured for every video. Without them the player cannot
    seek, and it downloads the whole file before the first frame — which is what
    `Accept-Ranges: none` used to force here.
    """
    item = await get_vault_item(db, item_id)
    if not item or not item.media_path:
        raise HTTPException(status_code=404, detail="Vault media not found")
    storage = get_storage()
    try:
        size = item.media_size or storage.get_file_size(item.media_path)
        seekable = storage.is_seekable_encrypted(item.media_path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Vault media file is missing") from exc

    media_type = item.media_mime or "video/mp4"
    range_header = request.headers.get("range")

    if range_header and size:
        span = _parse_byte_range(range_header, size)
        if span is None:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
            )
        start, end = span
        body = _iter_media(storage, item.media_path, start, end - start + 1, seekable)
        return StreamingResponse(
            body,
            status_code=206,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(size) if size else "",
        "Cache-Control": "private, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }
    headers = {key: value for key, value in headers.items() if value}
    return StreamingResponse(
        _iter_media(storage, item.media_path, 0, size, seekable),
        media_type=media_type,
        headers=headers,
    )


def _parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single `bytes=` range. Multi-range requests are answered whole."""
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].split(",")[0].strip()
    if "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    try:
        if not first:
            # A suffix range: the final N bytes.
            length = int(last)
            if length <= 0:
                return None
            start = max(0, size - length)
            return start, size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or start < 0:
        return None
    return start, min(end, size - 1)


def _iter_media(storage, path: str, start: int, length: int, seekable: bool):
    """Yield the requested plaintext bytes without ever holding the whole file."""
    if seekable:
        yield from storage.read_seekable_range(path, start, length)
        return
    with storage.get_file_stream(path) as stream:
        stream.seek(start)
        remaining = length
        while remaining > 0:
            block = stream.read(min(remaining, 1024 * 1024))
            if not block:
                break
            remaining -= len(block)
            yield block


@router.get("/api/vault/items/{item_id}/thumbnail", include_in_schema=False)
async def get_item_thumbnail(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Serve the poster a media download stored, when there is one."""
    item = await get_vault_item(db, item_id)
    path = item.media_thumbnail_path if item else None
    if not path:
        raise HTTPException(status_code=404, detail="Vault thumbnail not found")
    storage = get_storage()
    try:
        # Posters written before the encryption change are still plaintext on
        # disk, so the reader has to accept both rather than assume an envelope.
        content = await asyncio.to_thread(storage.read_maybe_encrypted, path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Vault thumbnail file is missing") from exc
    return Response(
        content=content,
        # Derived from the stored name: the endpoint used to claim every poster
        # was a JPEG, which broke a PNG served with an image/png body.
        media_type=media_type_for(path),
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/vault/items/{item_id}", response_model=VaultItemResponse)
async def get_item_by_id(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Get single vault item details."""
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    collection = await collection_for(db, item.collection_id)
    locked = False
    if is_sealed_collection(collection):
        private_key = await data_key_for(collection, unlock_token)
        if private_key is None:
            locked = True
        else:
            open_item(private_key, item)
    # The media flags are derived, not columns, so they have to be filled in.
    serialized = VaultItemResponse.model_validate(item).model_dump()
    _apply_media_state(serialized, item)
    return _apply_lock_state(serialized, item, locked=locked)


@router.get("/api/vault/items/{item_id}/preview", include_in_schema=False)
async def get_item_preview(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Proxy a remote bookmark image so an offline package remains self-contained."""
    item = await get_vault_item(db, item_id)
    if not item or not item.og_image or not item.og_image.startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail="Vault preview not found")
    try:
        content, content_type, _ = await asyncio.to_thread(
            fetch_bytes_checked,
            item.og_image,
            max_redirects=4,
            max_bytes=8 * 1024 * 1024,
            allowed_content_prefixes=("image/",),
            https_only=False,
        )
    except (RemoteFetchError, OSError) as exc:
        raise HTTPException(status_code=502, detail="Could not fetch Vault preview") from exc
    return Response(
        content=content,
        media_type="application/octet-stream" if content_type == "image/svg+xml" else content_type,
        headers={
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/api/vault/items/{item_id}/image", include_in_schema=False)
async def get_item_image(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Serve a Vault image so list responses stay light.

    Images live in encrypted storage; rows written before that still carry a
    data URL and are served from the column.
    """
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    decoded = image_bytes(item)
    if not decoded:
        raise HTTPException(status_code=404, detail="Vault image not found")
    payload, media_type = decoded
    return Response(payload, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})


@router.patch("/api/vault/items/{item_id}", response_model=VaultItemResponse)
async def update_item(
    item_id: int,
    update_in: VaultItemUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Update vault item properties.

    A sealed item is opened, edited and re-sealed in one transaction. While its
    Vault is locked the contents cannot be edited at all — only the public alias
    can, since that field is readable by design.
    """
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    collection = await collection_for(db, item.collection_id)
    if is_sealed_collection(collection):
        changed = set(update_in.model_dump(exclude_unset=True))
        private_key = await data_key_for(collection, unlock_token)
        if private_key is None:
            if changed - {"public_title"}:
                raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы изменить содержимое")
            updated = await update_vault_item(db, item, update_in)
            return _apply_lock_state(_serialize_full_item(updated), updated, locked=True)
        updated = await update_sealed_item(
            db, item, update_in, private_key, require_inbox_public_key(collection)
        )
        return _apply_lock_state(_serialize_full_item(updated), updated, locked=False)

    try:
        updated = await update_vault_item(db, item, update_in)
    except VaultMoveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _apply_lock_state(_serialize_full_item(updated), updated, locked=False)


@router.delete("/api/vault/items/{item_id}")
async def delete_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Delete a vault item."""
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    await delete_vault_item(db, item)
    return {"status": "ok", "message": "Item deleted"}


@router.post("/api/vault/items/{item_id}/pin", response_model=VaultItemResponse)
async def toggle_pin(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Toggle pin status of a vault item."""
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    updated = await toggle_pin_item(db, item)
    return updated


@router.post("/api/vault/items/{item_id}/archive", response_model=VaultItemResponse)
async def toggle_archive(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Toggle archive status of a vault item."""
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    updated = await toggle_archive_item(db, item)
    return updated


@router.post("/api/vault/items/{item_id}/increment-progress", response_model=VaultItemResponse)
async def increment_progress(
    item_id: int,
    step: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Quick increment episode/chapter progress."""
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    updated = await increment_item_progress(db, item, step=step)
    return updated


@router.post("/api/vault/fetch-meta")
async def fetch_meta_url(
    payload: dict,
    user=Depends(get_current_user),
):
    """Scrape OpenGraph metadata for a URL."""
    if not settings.ALLOW_REMOTE_METADATA_FETCH:
        raise HTTPException(status_code=403, detail="Remote metadata fetching is disabled")
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    meta = await fetch_url_metadata(url)
    return meta


@router.get("/api/vault/random", response_model=VaultItemResponse | None)
async def get_random_item(
    entry_type: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Get a random item from Vault for rediscovery."""
    items = await list_vault_items(session=db, entry_type=entry_type, is_archived=False, limit=500)
    if not items:
        return None
    return random.choice(items)


@router.get("/api/vault/stats", response_model=VaultStatsResponse)
async def get_stats(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Get summary statistics."""
    stats = await get_vault_stats(db)
    return stats


# ── Collections API ───────────────────────────────────────


@router.get("/api/vault/collections", response_model=list[VaultCollectionResponse])
async def get_collections(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """List all collection folders."""
    colls = await list_collections(db)
    locked = await locked_collection_ids(db, unlock_token)
    return [_serialize_collection(collection, locked=collection.id in locked) for collection in colls]


@router.post("/api/vault/collections", response_model=VaultCollectionResponse)
async def create_new_collection(
    coll_in: VaultCollectionCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Create a collection, optionally a sealed one.

    A sealed collection is created already unlocked: the caller supplied the
    passphrase in this same request, so making them type it twice would be noise.
    """
    if coll_in.passphrase:
        collection = await create_sealed_collection(
            db,
            coll_in.name,
            coll_in.passphrase,
            description=coll_in.description,
            color=coll_in.color,
            icon=coll_in.icon,
            public_name=coll_in.public_name or DEFAULT_SEALED_ALIAS,
        )
    else:
        collection = await create_collection(db, coll_in)
    return _serialize_collection(collection, locked=False)


def _serialize_collection(collection, *, locked: bool) -> dict:
    """Show the alias, not the name, while a sealed collection is locked.

    The caller decides `locked`: asking Redis once per collection would cost a
    round-trip per row of the sidebar, when one call per request is enough.
    """
    locked = bool(is_sealed_collection(collection) and locked)
    alias = collection.public_name or DEFAULT_SEALED_ALIAS
    payload = VaultCollectionResponse.model_validate(collection).model_dump()
    payload["is_encrypted"] = bool(collection.is_encrypted)
    payload["public_name"] = alias if collection.is_encrypted else None
    payload["is_locked"] = bool(locked)
    if locked:
        payload["name"] = alias
        payload["description"] = None
    return payload


@router.post("/api/vault/collections/{coll_id}/unlock", response_model=VaultUnlockResponse)
async def unlock_collection_route(
    coll_id: int,
    body: VaultUnlockRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Supply the passphrase and get back the token that opens it in this tab.

    The token is the whole point: it stays in the page's memory, is not a cookie
    and is not in Redis, so reloading the tab asks for the password again.
    """
    collection = await db.get(VaultCollection, coll_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="Vault not found")
    if not is_sealed_collection(collection):
        raise HTTPException(status_code=400, detail="This Vault is not sealed")
    try:
        token = await unlock_collection(collection, body.passphrase, unlock_token)
    except VaultUnlockError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return VaultUnlockResponse(
        collection_id=collection.id,
        name=collection.name,
        unlock_token=token,
        unlocked_collections=[collection.id],
    )


@router.post("/api/vault/collections/{coll_id}/lock", response_model=VaultLockResponse)
async def lock_collection_route(
    coll_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Forget this tab's key. Other tabs that unlocked it keep theirs."""
    await lock_collection(coll_id, unlock_token)
    return VaultLockResponse(collection_id=coll_id)


@router.get("/api/vault/lock-state")
async def vault_lock_state(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Which sealed collections are open right now."""
    sealed = await sealed_collection_ids(db)
    locked = await locked_collection_ids(db, unlock_token)
    return {"sealed": sorted(sealed), "locked": sorted(locked), "unlocked": sorted(sealed - locked)}


@router.post("/api/vault/items/move", response_model=VaultItemResponse)
async def move_item(
    payload: VaultItemMove,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Put a card where it was dropped, between two of its neighbours.

    Structural, so it works on a sealed collection too: a card's place in the
    grid is not what the passphrase protects.
    """
    try:
        item = await reorder_card(db, payload.item_id, payload.before_id, payload.after_id)
    except VaultOrderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _serialize_full_item(item)


@router.post("/api/vault/collections/move", response_model=VaultCollectionResponse)
async def move_collection(
    payload: VaultCollectionMove,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Nest a space under another one, or move it among its siblings."""
    try:
        collection = await reorder_space(
            db, payload.collection_id, payload.parent_id, payload.before_id, payload.after_id
        )
    except VaultCollectionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VaultOrderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    locked = await locked_collection_ids(db, unlock_token)
    return _serialize_collection(collection, locked=collection.id in locked)


@router.post("/api/vault/collections/{coll_id}/dissolve")
async def dissolve_collection(
    coll_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Dissolve a folder: its contents move up and keep the folder's place."""
    try:
        moved = await dissolve_space(db, coll_id)
    except VaultCollectionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VaultDissolveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", **moved}


@router.get("/api/vault/collections/{coll_id}/children", response_model=list[VaultCollectionResponse])
async def get_collection_children(
    coll_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """The spaces nested inside one, in their saved order."""
    children = await list_child_spaces(db, coll_id)
    locked = await locked_collection_ids(db, unlock_token)
    return [_serialize_collection(child, locked=child.id in locked) for child in children]


@router.post("/api/vault/collections/merge")
async def merge_collections_route(
    payload: VaultCollectionMerge,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Drag-and-drop merge: move every card into another workspace, delete the emptied one."""
    try:
        moved = await merge_collections(db, payload.from_id, payload.into_id)
    except VaultCollectionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VaultMergeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "moved": moved, "into_id": payload.into_id}


@router.delete("/api/vault/collections/{coll_id}")
async def delete_coll(
    coll_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Delete a collection."""
    await delete_collection(db, coll_id)
    return {"status": "ok", "message": "Collection deleted"}


@router.get("/api/vault/entity-meta")
async def get_entity_meta(
    entity_type: str = Query(...),
    entity_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Soft integration endpoint to resolve metadata from external modules."""
    meta = await resolve_soft_entity_info(db, entity_type, entity_id)
    return meta or {}


# ── NSP Sync Manifest ─────────────────────────────────────


@router.get("/api/vault/sync-manifest")
async def get_vault_sync_manifest(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    hybrid: bool = True,
):
    """
    Generate a NetOutpost sync manifest for the full Vault module.
    Allows offline access to all Vault bookmarks, ratings and notes via NSP container.
    """
    pkg_id = VAULT_PACKAGE_ID
    package_query = f"package_id={pkg_id}"

    resources = [
        {"url": f"/vault/dashboard?{package_query}", "type": "html"},
        {"url": f"/api/vault/stats?{package_query}", "type": "json"},
        {"url": f"/api/vault/collections?{package_query}", "type": "json"},
        {"url": f"/api/vault/package-items?{package_query}", "type": "json"},
        {"url": "/static/tailwind.css", "type": "css"},
        {"url": "/static/htmx.min.js", "type": "js"},
    ]

    from app.core.packages_router import make_package_manifest

    manifest = make_package_manifest(
        module_id="vault",
        package_id=pkg_id,
        package_title="Vault — Личный архив",
        root_url=f"/vault/dashboard?{package_query}",
        resources=resources,
    )

    if hybrid:
        from app.core.packages_router import make_hybrid_manifest

        return make_hybrid_manifest(pkg_id, manifest)
    return manifest
