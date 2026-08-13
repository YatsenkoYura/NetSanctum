import random

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
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
    limit: int = Query(100, le=500),
    offset: int = Query(0),
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
    return items


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

    resources = [
        {"url": "/vault/dashboard", "type": "html"},
        {"url": "/api/vault/items?limit=500&is_archived=false", "type": "json"},
        {"url": "/api/vault/stats", "type": "json"},
        {"url": "/api/vault/collections", "type": "json"},
        {"url": "/static/tailwind.css", "type": "css"},
        {"url": "/static/htmx.min.js", "type": "js"},
    ]

    # Include OG images for bookmarks that have them
    items = await list_vault_items(session=db, is_archived=False, limit=500)
    for item in items:
        if item.og_image:
            resources.append({"url": item.og_image, "type": "image"})

    from app.core.packages_router import make_package_manifest

    manifest = make_package_manifest(
        module_id="vault",
        package_id=pkg_id,
        package_title="Vault — Личный архив",
        root_url="/vault/dashboard",
        resources=resources,
    )

    if hybrid:
        from app.core.packages_router import make_hybrid_manifest

        return make_hybrid_manifest(pkg_id, manifest)
    return manifest
