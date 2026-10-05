import asyncio
import random

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import update
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
from app.modules.vault.crypto import (
    VaultUnlockError,
    WeakPassphraseError,
    inbox_pub_fingerprint,
)
from app.modules.vault.images import (
    LOCAL_IMAGE_PREFIXES,
    decode_data_image,
    externalize_image,
    has_image,
    image_bytes,
    media_type_for,
)
from app.modules.vault.models import VaultCollection, VaultItem
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
    VaultSpaceDeleteRequest,
    VaultStatsResponse,
    VaultUnlockRequest,
    VaultUnlockResponse,
)
from app.modules.vault.sealing import (
    DEFAULT_ITEM_ALIAS,
    DEFAULT_SEALED_ALIAS,
    SEALED_FIELDS,
    SEALED_ZEROED_FIELDS,
    VaultMoveError,
    bump_media_epoch,
    clear_unlock_failures,
    collection_for,
    create_sealed_collection,
    data_key_for,
    file_key_for,
    file_key_for_write,
    inbox_public_key,
    increment_sealed_progress,
    is_sealed_collection,
    lock_collection,
    locked_collection_ids,
    media_epoch,
    move_sealed_item,
    open_collection_payloads,
    open_item,
    open_items,
    record_unlock_failure,
    require_inbox_public_key,
    seal_item,
    sealed_collection_ids,
    sign_file_url,
    unlock_backoff_seconds,
    unlock_collection,
    update_sealed_item,
    verify_file_url,
    verify_space_passphrase,
)
from app.modules.vault.services import (
    VaultCollectionNotFoundError,
    VaultDissolveError,
    VaultMergeError,
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
    place_new_space,
    queue_video_download,
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
    opened = await open_collection_payloads(collections, unlock_token)
    serializable = [
        _serialize_collection(collection, locked=collection.id in locked, opened=opened)
        for collection in collections
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
    serialized = []
    for item in items:
        payload = _apply_lock_state(
            # Always a dict: handing this an ORM row would write the alias into a
            # column the next commit flushes.
            _serialize_full_item(item) if include_images else _serialize_list_item(item),
            item,
            locked=item.collection_id in locked,
        )
        serialized.append(await attach_media_url(payload, item, locked=item.collection_id in locked))
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
    # The signed player URL is attached by `attach_media_url`, not here: minting
    # needs the media epoch, and this stays pure for the tests that pin it.
    serialized["media_url"] = None
    if sealed and locked:
        serialized["title"] = alias
        # Driven off SEALED_FIELDS rather than a hand-written list: a field added
        # to the sealed set must not silently start leaking here.
        #
        # Zeroed rather than nulled where the column is NOT NULL, using the same
        # table the write path uses. These two are integers the response schema
        # declares as required, so blanking them to None did not hide a value — it
        # turned every read, create and edit of a card in a locked space into a
        # 500.
        for field in SEALED_FIELDS:
            serialized[field] = SEALED_ZEROED_FIELDS.get(field)
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
    collection = await collection_for(db, item.collection_id)
    file_key = None
    locked = False
    if is_sealed_collection(collection):
        # Unlocked: the picture goes into storage under the file key. Locked
        # (a blind write): the data URL stays and is sealed into the payload
        # below — the locked vault has no file key to store it under, and the
        # first save while unlocked moves it out.
        file_key, locked = await file_key_for_write(collection, unlock_token)
        if file_key is not None and externalize_image(item, key=file_key, sealed=True):
            await db.commit()
            await db.refresh(item)
    elif externalize_image(item):
        await db.commit()
        await db.refresh(item)
    if is_sealed_collection(collection):
        item.public_title = item_in.public_title or DEFAULT_ITEM_ALIAS
        seal_item(item, require_inbox_public_key(collection))
        await db.commit()
        await db.refresh(item)
    payload = _apply_lock_state(_serialize_full_item(item), item, locked=locked)
    return await attach_media_url(payload, item, locked=locked)


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
        # A capture into a sealed collection must be sealed too. This used to be
        # missing entirely, which meant the extension wrote screenshots, titles and
        # URLs into a locked vault in the clear, silently.
        collection = await collection_for(db, getattr(item, "collection_id", None))
        if not is_sealed_collection(collection):
            externalize_image(item)
        # Else a blind write: the extension never holds the passphrase, so the
        # vault is locked from here. The picture stays a data URL and is sealed
        # into the payload below — there is no file key to store it under
        # without an unlock, and the shared key would protect a blind write
        # less than an unlocked one.
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
    # For a sealed collection the response must not hand the content back: sealing
    # blanked `title`, so echoing it would send an empty name, and `image_path` was
    # never sealed, so handing out its URL would publish the file. The alias is the
    # only name an unauthenticated caller may see.
    sealed = is_sealed_collection(collection)
    return VaultCaptureResponse(
        item_id=item.id,
        kind=capture_in.kind,
        title=item.public_title if sealed else item.title,
        image_url=None if sealed else (f"/api/vault/items/{item.id}/image" if has_image(item) else None),
        task_id=item.related_entity_id if capture_in.kind == "video" else None,
        message=messages[capture_in.kind],
    )


@router.get("/api/vault/items/{item_id}/media", include_in_schema=False)
async def get_item_media(
    item_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
    sig: str | None = Query(None),
    exp: str | None = Query(None),
):
    """Stream the video a media card owns, so the file lives with the entry.

    Range requests are honoured for every video. Without them the player cannot
    seek, and it downloads the whole file before the first frame — which is what
    `Accept-Ranges: none` used to force here.
    """
    item = await get_vault_item(db, item_id)
    if not item or not item.media_path:
        raise HTTPException(status_code=404, detail="Vault media not found")
    _assert_media_path_belongs_to(item, item.media_path)
    collection = await collection_for(db, item.collection_id)
    await _require_media_access(collection, item, unlock_token, sig, exp)
    # The file key travels separately from the access check: sealed videos are
    # stored under the collection's file key, and the range reader below must
    # open them under exactly that key — never falling through to the
    # application keys.
    file_key = await file_key_for(collection, unlock_token) if is_sealed_collection(collection) else None
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
        body = _iter_media(storage, item.media_path, start, end - start + 1, seekable, key=file_key)
        return StreamingResponse(
            body,
            status_code=206,
            media_type=media_type,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(size) if size else "",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    headers = {key: value for key, value in headers.items() if value}
    return StreamingResponse(
        _iter_media(storage, item.media_path, 0, size, seekable, key=file_key),
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


# NOTE: the file endpoints used to serve without checking the lock at all.
# `<img>` and `<video>` cannot send `X-Vault-Unlock`, which is why the check was
# missing — but a locked vault's files served to anyone with the URL is a hole,
# not a trade-off. Now every file endpoint gates on the lock: images, posters
# and previews take the header through an authorized `fetch()` into a blob URL,
# and the player gets a short-lived signed URL, because it cannot do that and
# still seek. The signed URL is rendered only for an unlocked card.
async def _require_file_access(collection, unlock_token: str | None) -> bytes | None:
    """The file key for this read, or a refusal.

    Plain collections need nothing: their files were never sealed. A sealed
    collection needs the tab's unlock, full stop — a URL cannot carry the
    session-side file key, so there is no signed form of an image or a poster.
    """
    if not is_sealed_collection(collection):
        return None
    if unlock_token and await data_key_for(collection, unlock_token) is not None:
        return await file_key_for(collection, unlock_token)
    raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы открыть файл")


def _assert_media_path_belongs_to(item, path: str) -> None:
    """A sealed media path has to name the card that is asking for it.

    The envelope already binds its own path, so a path moved between rows would
    fail to decrypt rather than open the wrong file — this is the check that says
    so out loud instead of returning a decryption error, and it stops a row that
    points at somebody else's object from being served at all.

    The collection segment is not compared: a card can be moved between sealed
    spaces, and its media moves with it without being re-encrypted. The item
    segment is the row's own id and never changes.
    """
    from app.modules.vault.tasks import sealed_media_owner

    owner = sealed_media_owner(path)
    if owner is None:
        # A legacy or plain-collection object, encrypted under the application
        # key. Served as before; documented in the README as a residual risk.
        return
    _collection_id, claimed_item = owner
    if claimed_item != item.id:
        raise HTTPException(status_code=403, detail="The stored file does not belong to this card")


async def _require_media_access(
    collection, item, unlock_token: str | None, signature: str | None, expires: str | None
) -> None:
    """The player's gate: the unlock header, or a signed URL that is still alive.

    The signature binds the collection, the item, the stored path, the expiry
    and the media epoch — a lock since minting voids it, and a signature lifted
    from another file or collection does not verify. Images have no signed
    form: their key lives session-side, where no URL can reach it.
    """
    if not is_sealed_collection(collection):
        return
    if unlock_token and await data_key_for(collection, unlock_token) is not None:
        return
    if await verify_file_url(
        collection.id if collection is not None else None,
        item.id,
        "media",
        item.media_path,
        expires,
        signature,
    ):
        return
    raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы открыть файл")


async def attach_media_url(serialized: dict, item, *, locked: bool) -> dict:
    """A signed player URL for an unlocked sealed card that holds a video.

    Minted per serialization, never stored: the epoch inside dies with the
    next lock. Plain collections need nothing — their video URL carries no
    authorization. Async for the epoch read, so `_apply_lock_state` stays pure
    for the tests that pin it.
    """
    serialized["media_url"] = None
    if locked or not getattr(item, "sealed_payload", None) or not getattr(item, "media_path", None):
        return serialized
    epoch = await media_epoch(getattr(item, "collection_id", None))
    expires, signature = sign_file_url(item.collection_id, item.id, "media", item.media_path, epoch)
    serialized["media_url"] = f"/api/vault/items/{item.id}/media?exp={expires}&sig={signature}"
    return serialized


def _iter_media(storage, path: str, start: int, length: int, seekable: bool, *, key: bytes | None = None):
    """Yield the requested plaintext bytes without ever holding the whole file.

    Chunked objects decrypt only the covered chunks under `key`. Anything else
    is either a single-blob envelope — decrypted whole (videos are always
    chunked, so this is the small-file path) — or a legacy plaintext object
    served as stored.
    """
    if seekable:
        yield from storage.read_seekable_range(path, start, length, key=key)
        return
    if storage.looks_encrypted(path):
        plaintext = storage.get_file_decrypted(path, key=key)
    else:
        with storage.get_file_stream(path) as stream:
            plaintext = stream.read()
    yield plaintext[start : start + length]


@router.get("/api/vault/items/{item_id}/thumbnail", include_in_schema=False)
async def get_item_thumbnail(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Serve the poster a media download stored, when there is one."""
    item = await get_vault_item(db, item_id)
    if not item or not item.media_thumbnail_path:
        raise HTTPException(status_code=404, detail="Vault thumbnail not found")
    path = item.media_thumbnail_path
    _assert_media_path_belongs_to(item, path)
    file_key = await _require_file_access(await collection_for(db, item.collection_id), unlock_token)
    storage = get_storage()
    try:
        if file_key is not None:
            # Worker posters from an unlocked queueing tab carry the file key;
            # older ones the application key, oldest ones nothing at all. The
            # file key is tried exactly once, with no legacy rotation behind
            # it — exactly one key can ever open the file either way.
            try:
                content = await asyncio.to_thread(storage.get_file_decrypted, path, key=file_key)
            except FileNotFoundError:
                raise
            except ValueError:
                # Posters written before the encryption change are still
                # plaintext on disk, so the reader has to accept both rather
                # than assume an envelope.
                content = await asyncio.to_thread(storage.read_maybe_encrypted, path)
        else:
            content = await asyncio.to_thread(storage.read_maybe_encrypted, path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Vault thumbnail file is missing") from exc
    return Response(
        content=content,
        # Derived from the stored name: the endpoint used to claim every poster
        # was a JPEG, which broke a PNG served with an image/png body.
        media_type=media_type_for(path),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
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
    payload = _apply_lock_state(serialized, item, locked=locked)
    return await attach_media_url(payload, item, locked=locked)


@router.get("/api/vault/items/{item_id}/preview", include_in_schema=False)
async def get_item_preview(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Proxy a remote bookmark image so an offline package remains self-contained."""
    item = await get_vault_item(db, item_id)
    if not item or not item.og_image or not item.og_image.startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail="Vault preview not found")
    # The proxied address is the card's content for a sealed item, so it gets
    # the same gate as the files. Nothing keyed is involved — the bytes are
    # remote — which is why the check is access, not decryption.
    await _require_file_access(await collection_for(db, item.collection_id), unlock_token)
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
    unlock_token: str = UNLOCK_HEADER,
):
    """Serve a Vault image so list responses stay light.

    Images live in encrypted storage; rows written before that still carry a
    data URL and are served from the column. A sealed collection's image needs
    the tab's unlock: the page fetches it with the header into a blob URL,
    because no URL can carry the session-side file key.
    """
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    file_key = await _require_file_access(await collection_for(db, item.collection_id), unlock_token)
    decoded = await asyncio.to_thread(image_bytes, item, file_key=file_key)
    if not decoded:
        raise HTTPException(status_code=404, detail="Vault image not found")
    payload, media_type = decoded
    return Response(payload, media_type=media_type, headers={"Cache-Control": "private, no-store"})


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
            payload = _apply_lock_state(_serialize_full_item(updated), updated, locked=True)
            return await attach_media_url(payload, updated, locked=True)
        # A move between sealed spaces re-seals under the *target's* key, or the
        # card would arrive as ciphertext nothing in that space can open.
        if "collection_id" in changed:
            target = await collection_for(db, update_in.collection_id) if update_in.collection_id else None
            try:
                updated = await move_sealed_item(db, item, target, private_key)
            except VaultMoveError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            payload = _apply_lock_state(_serialize_full_item(updated), updated, locked=False)
            return await attach_media_url(payload, updated, locked=False)
        updated = await update_sealed_item(
            db,
            item,
            update_in,
            private_key,
            require_inbox_public_key(collection),
            file_key=(await file_key_for_write(collection, unlock_token))[0],
        )
        payload = _apply_lock_state(_serialize_full_item(updated), updated, locked=False)
        return await attach_media_url(payload, updated, locked=False)

    try:
        updated = await update_vault_item(db, item, update_in)
    except VaultMoveError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = _apply_lock_state(_serialize_full_item(updated), updated, locked=False)
    return await attach_media_url(payload, updated, locked=False)


@router.delete("/api/vault/items/{item_id}")
async def delete_item(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Delete a vault item.

    Destroying a sealed card needs the vault unlocked, like destroying the
    space does: the lock is the only confirmation the owner holds the
    passphrase, and a session cookie alone must not be enough to wipe a vault.
    Pin/archive stay structural and unlocked; deletion is irreversible.
    """
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    collection = await collection_for(db, item.collection_id)
    if is_sealed_collection(collection) and await data_key_for(collection, unlock_token) is None:
        raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы удалить карточку")
    await delete_vault_item(db, item)
    return {"status": "ok", "message": "Item deleted"}


@router.post("/api/vault/items/{item_id}/pin", response_model=VaultItemResponse)
async def toggle_pin(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Toggle pin status of a vault item. Structural, so no unlock is needed."""
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
    """Toggle archive status of a vault item. Structural, so no unlock is needed."""
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
    unlock_token: str = UNLOCK_HEADER,
):
    """Quick increment episode/chapter progress.

    Progress is a sealed field, so a locked card refuses (423) and an unlocked
    one increments inside its payload and re-seals — writing the zeroed column
    directly would be lost on the next unlock.
    """
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    collection = await collection_for(db, item.collection_id)
    if is_sealed_collection(collection):
        private_key = await data_key_for(collection, unlock_token)
        if private_key is None:
            raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы изменить прогресс")
        updated = await increment_sealed_progress(
            db, item, private_key, require_inbox_public_key(collection), step=step
        )
        payload = _apply_lock_state(_serialize_full_item(updated), updated, locked=False)
        return await attach_media_url(payload, updated, locked=False)
    updated = await increment_item_progress(db, item, step=step)
    return updated


@router.post("/api/vault/items/{item_id}/video/retry")
async def retry_video_download(
    item_id: int,
    quality: str = Query("720"),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Start the video download for a sealed card, from an unlocked tab.

    The only way a sealed collection's video gets downloaded at all. A capture
    cannot: the extension holds no passphrase, so it creates the card and stops.
    This lends the tab's token through the sealed handoff, and the worker stores
    the file under the collection's file key at a fresh path that names the row.

    Also the answer to a download that was refused because the vault locked while
    the card sat waiting: the retry is the same call with nothing else to do.
    """
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    if item.node_type != "video":
        raise HTTPException(status_code=422, detail="Только видеозапись можно поставить на загрузку")
    collection = await collection_for(db, item.collection_id)
    worker_token = None
    url, title = item.url, item.title
    if is_sealed_collection(collection):
        private_key = await data_key_for(collection, unlock_token)
        if private_key is None:
            raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы загрузить видео")
        open_item(private_key, item)
        url, title = item.url, item.title
        worker_token = unlock_token
    if not url:
        raise HTTPException(status_code=422, detail="У записи нет адреса для загрузки")
    # `open_item` above restored the sealed columns in memory; committing that
    # state would write the card back in the clear. The rollback drops it, and
    # the status moves through a statement that touches nothing else.
    await db.rollback()
    await db.execute(update(VaultItem).where(VaultItem.id == item.id).values(media_status="queued"))
    await db.commit()
    task_id = await queue_video_download(
        db, item.id, url, quality=quality, title=title, unlock_token=worker_token
    )
    if task_id is None:
        raise HTTPException(status_code=503, detail="Не удалось поставить загрузку в очередь")
    return {"status": "ok", "task_id": task_id}


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
    unlock_token: str = UNLOCK_HEADER,
):
    """Get a random item from Vault for rediscovery.

    Masked like the list endpoint: a sealed card served here is an alias with
    no bytes and no player URL, never its content.
    """
    items = await list_vault_items(session=db, entry_type=entry_type, is_archived=False, limit=500)
    if not items:
        return None
    item = random.choice(items)
    collection = await collection_for(db, item.collection_id)
    locked = False
    if is_sealed_collection(collection):
        private_key = await data_key_for(collection, unlock_token)
        if private_key is None:
            locked = True
        else:
            open_item(private_key, item)
    serialized = VaultItemResponse.model_validate(item).model_dump()
    _apply_media_state(serialized, item)
    payload = _apply_lock_state(serialized, item, locked=locked)
    return await attach_media_url(payload, item, locked=locked)


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
    opened = await open_collection_payloads(colls, unlock_token)
    return [
        _serialize_collection(collection, locked=collection.id in locked, opened=opened)
        for collection in colls
    ]


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
        try:
            collection = await create_sealed_collection(
                db,
                coll_in.name,
                coll_in.passphrase,
                description=coll_in.description,
                color=coll_in.color,
                icon=coll_in.icon,
                public_name=coll_in.public_name or DEFAULT_SEALED_ALIAS,
                parent_id=coll_in.parent_id,
            )
        except WeakPassphraseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # A plain space gets its place among its siblings inside create_collection;
        # the sealed path needs it here, because `sealing` cannot import `services`.
        await place_new_space(db, collection)
        await db.commit()
        await db.refresh(collection)
    else:
        collection = await create_collection(db, coll_in)
    return _serialize_collection(collection, locked=False)


def _serialize_collection(collection, *, locked: bool, opened: dict | None = None) -> dict:
    """Show the alias, not the name, while a sealed collection is locked.

    The caller decides `locked`: asking Redis once per collection would cost a
    round-trip per row of the sidebar, when one call per request is enough. `opened`
    carries the collection's own metadata for the unlocked sealed ones, read back
    from their payload in the same batch — without it a collection that was sealed
    with its description has nothing to show for it once it is unlocked.
    """
    locked = bool(is_sealed_collection(collection) and locked)
    alias = collection.public_name or DEFAULT_SEALED_ALIAS
    payload = VaultCollectionResponse.model_validate(collection).model_dump()
    payload["is_encrypted"] = bool(collection.is_encrypted)
    payload["public_name"] = alias if collection.is_encrypted else None
    payload["is_locked"] = bool(locked)
    public_key = inbox_public_key(collection)
    payload["key_fingerprint"] = (
        inbox_pub_fingerprint(public_key) if public_key is not None and collection.is_encrypted else None
    )
    if locked:
        payload["name"] = alias
        payload["description"] = None
    elif opened and collection.id in opened:
        fields = opened[collection.id]
        if "description" in fields:
            payload["description"] = fields["description"]
    return payload


@router.post("/api/vault/collections/{coll_id}/unlock", response_model=VaultUnlockResponse)
async def unlock_collection_route(
    coll_id: int,
    body: VaultUnlockRequest,
    request: Request,
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
    client_ip = request.client.host if request.client else ""
    # Before the KDF, not after: each derivation is ~350ms and 64 MiB, so counting
    # failures after deriving would let anyone spend the server's memory by guessing.
    wait = await unlock_backoff_seconds(collection.id, client_ip)
    if wait > 0:
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток — подождите перед следующей",
            headers={"Retry-After": str(wait)},
        )
    try:
        token = await unlock_collection(collection, body.passphrase, unlock_token, session=db)
    except VaultUnlockError as exc:
        await record_unlock_failure(collection.id, client_ip)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    await clear_unlock_failures(collection.id, client_ip)
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
    # Every player URL ever issued for this collection dies here: the epoch
    # inside no longer matches, so the next Range request fails and playback
    # freezes at the end of the buffer.
    await bump_media_epoch(coll_id)
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
    unlock_token: str = UNLOCK_HEADER,
):
    """Put a card where it was dropped, between two of its neighbours.

    Structural, so it works on a sealed collection too: a card's place in the
    grid is not what the passphrase protects. An unlocked sealed card is opened
    first, so the response carries its content rather than its blanked columns.
    """
    try:
        item = await reorder_card(db, payload.item_id, payload.before_id, payload.after_id)
    except VaultOrderError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    collection = await collection_for(db, item.collection_id)
    locked = False
    if is_sealed_collection(collection):
        private_key = await data_key_for(collection, unlock_token)
        if private_key is None:
            locked = True
        else:
            open_item(private_key, item)
    serialized = _apply_lock_state(_serialize_full_item(item), item, locked=locked)
    return await attach_media_url(serialized, item, locked=locked)


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
    opened = await open_collection_payloads(children, unlock_token)
    return [_serialize_collection(child, locked=child.id in locked, opened=opened) for child in children]


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
    request: Request,
    body: VaultSpaceDeleteRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Delete a space and everything in it.

    A sealed space asks for its passphrase first, and the passphrase is checked by
    opening it rather than accepted on its word. The space holds the only copy of
    whatever is in it — there is no backup path and no recovery — so being able to
    destroy one by typing its id is not a property worth having. A wrong passphrase
    gets the same answer as a wrong one anywhere else here, and nothing is
    removed.

    Plain spaces need no passphrase: nothing secret is lost by deleting them, and
    asking for one would be a prompt the owner cannot satisfy by looking at it.
    """
    collection = await db.get(VaultCollection, coll_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="Воркспейс не найден")

    if collection.is_encrypted:
        passphrase = (body.passphrase if body else None) or ""
        if not passphrase:
            raise HTTPException(status_code=422, detail="Нужен пароль для зашифрованного воркспейса")
        client_ip = request.client.host if request.client else ""
        try:
            await asyncio.to_thread(verify_space_passphrase, collection, passphrase)
        except VaultUnlockError:
            await record_unlock_failure(coll_id, client_ip)
            raise HTTPException(status_code=401, detail="Неверный пароль") from None
        except WeakPassphraseError:
            raise HTTPException(status_code=422, detail="Пароль слишком короткий") from None

    removed = await delete_collection(db, coll_id)
    return {
        "status": "ok",
        "message": "Воркспейс удалён",
        "cards": removed["cards"],
        "spaces": removed["spaces"],
    }


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


# ── Sealed offline package ──────────────────────────────────
# One package per sealed collection, generated on demand from an unlocked tab
# and never stored. See `app/modules/vault/sealed_package.py` and
# `docs/sealed-offline-packages.md` for the format and the guarantees.


@router.get("/api/vault/sealed/manifest")
async def get_sealed_sync_manifest(
    collection_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """Offline manifest for one sealed collection, ciphertext only.

    Needs the vault unlocked (the snapshot is built from opened cards), and a
    stored package wrapper (written by the last unlock — a collection never
    unlocked since the sealed columns landed answers 409, never a guess).
    Sealed resources carry no size or hash: fresh nonces per generation make
    them dynamic, so the client downloads them on every refresh.
    """
    from app.core.packages_router import make_hybrid_manifest, make_package_manifest
    from app.modules.vault.sealed_package import (
        require_sealed_collection,
        sealed_items_url,
        sealed_package_resources,
        sealed_package_title,
    )
    from app.modules.vault.sealing import sealed_package_id, sealed_package_sealing

    try:
        collection = await require_sealed_collection(db, collection_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Sealed Vault not found") from exc
    if await data_key_for(collection, unlock_token) is None:
        raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы выгрузить пакет")
    sealing = sealed_package_sealing(collection)
    if sealing is None:
        raise HTTPException(
            status_code=409,
            detail="Разблокируйте Vault ещё раз, чтобы подготовить зашифрованный пакет",
        )
    package_id = sealed_package_id(collection.id)
    manifest = make_package_manifest(
        module_id="vault",
        package_id=package_id,
        package_title=sealed_package_title(collection),
        root_url=sealed_items_url(collection.id, package_id),
        resources=await sealed_package_resources(db, collection),
    )
    manifest["sealing"] = sealing
    manifest = make_hybrid_manifest(package_id, manifest)
    manifest["sealing"] = sealing
    return manifest


@router.get("/api/vault/sealed/{collection_id}/items", include_in_schema=False)
async def get_sealed_items(
    collection_id: int,
    package_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """One collection's cards as a sealed JSON snapshot, generated per request."""
    from app.modules.vault.sealed_package import (
        build_sealed_items_plaintext,
        require_sealed_collection,
        seal_items_snapshot,
    )
    from app.modules.vault.sealing import package_dek_for, sealed_package_id

    if package_id != sealed_package_id(collection_id):
        raise HTTPException(status_code=400, detail="Invalid sealed package ID")
    try:
        collection = await require_sealed_collection(db, collection_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Sealed Vault not found") from exc
    private_key = await data_key_for(collection, unlock_token)
    if private_key is None:
        raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы выгрузить пакет")
    try:
        plaintext = await build_sealed_items_plaintext(db, collection, private_key)
        blob = seal_items_snapshot(
            package_dek_for(private_key, collection.id), package_id, collection.id, plaintext
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        # Opened cards were restored in memory; the rollback guarantees none of
        # that is ever flushed over sealed columns.
        await db.rollback()
    return Response(
        content=blob,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/vault/sealed/media/{item_id}", include_in_schema=False)
async def get_sealed_media(
    item_id: int,
    package_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    unlock_token: str = UNLOCK_HEADER,
):
    """One card's media re-sealed under its resource key, streamed chunk by chunk.

    Decrypts under the file key and seals under the resource key in transit —
    the plaintext exists only in the chunks flowing through this response.
    """
    from app.modules.vault.sealed_package import iter_sealed_media
    from app.modules.vault.sealing import derive_file_key, package_dek_for, sealed_package_id

    item = await get_vault_item(db, item_id)
    if not item or item.collection_id is None:
        raise HTTPException(status_code=404, detail="Vault media not found")
    if package_id != sealed_package_id(item.collection_id):
        raise HTTPException(status_code=400, detail="Invalid sealed package ID")
    collection = await collection_for(db, item.collection_id)
    if not is_sealed_collection(collection) or collection is None:
        raise HTTPException(status_code=404, detail="Vault media not found")
    private_key = await data_key_for(collection, unlock_token)
    if private_key is None:
        raise HTTPException(status_code=423, detail="Разблокируйте Vault, чтобы выгрузить пакет")
    if not (item.media_path or item.image_path):
        raise HTTPException(status_code=404, detail="Vault media not found")
    try:
        body = iter_sealed_media(
            package_dek_for(private_key, collection.id),
            package_id,
            item,
            derive_file_key(private_key, collection.id),
        )
        first = next(body)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Vault media file is missing") from exc

    def _stream():
        yield first
        yield from body

    return StreamingResponse(
        _stream(),
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


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
