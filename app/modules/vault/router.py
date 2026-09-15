import asyncio
import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked
from app.core.security import get_current_user
from app.core.templates import templates
from app.modules.vault.schemas import (
    VaultCollectionCreate,
    VaultCollectionResponse,
    VaultItemCreate,
    VaultItemResponse,
    VaultItemUpdate,
    VaultStatsResponse,
)
from app.modules.vault.services import (
    create_collection,
    create_vault_item,
    delete_collection,
    delete_vault_item,
    fetch_url_metadata,
    get_vault_item,
    get_vault_stats,
    increment_item_progress,
    list_collections,
    list_vault_items,
    resolve_soft_entity_info,
    toggle_archive_item,
    toggle_pin_item,
    update_vault_item,
)

router = APIRouter()
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
):
    """Serve the primary Vault personal scrapbook & tracker dashboard."""
    lang = await _get_lang(request)
    collections = await list_collections(db)
    stats = await get_vault_stats(db)

    return templates.TemplateResponse(
        request,
        "vault_dashboard.html",
        {
            "user": user,
            "lang": lang,
            "collections": collections,
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
    sort_by: str = Query("created_at"),
    sort_order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    package_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """List vault items with dynamic filter parameters."""
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
    if package_id and package_id != "vault_all":
        raise HTTPException(status_code=400, detail="Invalid Vault package ID")
    if not package_id:
        return items

    result = []
    for item in items:
        serialized = VaultItemResponse.model_validate(item).model_dump()
        if item.og_image and item.og_image.startswith(("http://", "https://")):
            serialized["og_image"] = f"/api/vault/items/{item.id}/preview?package_id={package_id}"
        result.append(serialized)
    return result


@router.post("/api/vault/items", response_model=VaultItemResponse)
async def create_item(
    item_in: VaultItemCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Create a new vault item (bookmark, rating, thought)."""
    item = await create_vault_item(db, item_in)
    return item


@router.get("/api/vault/items/{item_id}", response_model=VaultItemResponse)
async def get_item_by_id(
    item_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Get single vault item details."""
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    return item


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


@router.patch("/api/vault/items/{item_id}", response_model=VaultItemResponse)
async def update_item(
    item_id: int,
    update_in: VaultItemUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Update vault item properties."""
    item = await get_vault_item(db, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Vault item not found")
    updated = await update_vault_item(db, item, update_in)
    return updated


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
):
    """List all collection folders."""
    colls = await list_collections(db)
    return colls


@router.post("/api/vault/collections", response_model=VaultCollectionResponse)
async def create_new_collection(
    coll_in: VaultCollectionCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Create a new collection."""
    coll = await create_collection(db, coll_in)
    return coll


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
    pkg_id = "vault_all"
    package_query = f"package_id={pkg_id}"

    resources = [
        {"url": f"/vault/dashboard?{package_query}", "type": "html"},
        {"url": f"/api/vault/stats?{package_query}", "type": "json"},
        {"url": f"/api/vault/collections?{package_query}", "type": "json"},
        {"url": "/static/tailwind.css", "type": "css"},
        {"url": "/static/htmx.min.js", "type": "js"},
    ]

    # Include every page, including an empty terminal page when the count is a multiple of 500.
    items = []
    offset = 0
    while True:
        resources.append(
            {
                "url": (f"/api/vault/items?limit=500&offset={offset}&is_archived=false&{package_query}"),
                "type": "json",
            }
        )
        page = await list_vault_items(session=db, is_archived=False, limit=500, offset=offset)
        items.extend(page)
        if len(page) < 500:
            break
        offset += 500

    # Remote bookmark images are fetched through a local, SSRF-protected endpoint.
    for item in items:
        if item.og_image and item.og_image.startswith(("http://", "https://")):
            resources.append({"url": f"/api/vault/items/{item.id}/preview?{package_query}", "type": "image"})

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
