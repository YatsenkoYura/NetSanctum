"""
Settings module — HTTP router.

Endpoints:
    GET    /settings/              — list settings (filtered, paginated)
    POST   /settings/              — create or upsert a setting
    POST   /settings/bulk          — bulk upsert multiple settings
    GET    /settings/resolve       — resolve a key via scope hierarchy
    GET    /settings/{setting_id}  — get a single setting by ID
    PATCH  /settings/{setting_id}  — update a setting
    DELETE /settings/{setting_id}  — delete a setting
    DELETE /settings/module/{name} — delete all module settings
    GET    /settings/ui/panel      — HTMX fragment: settings panel
    POST   /settings/ui/add        — HTMX: add a setting from dashboard
    DELETE /settings/ui/{id}       — HTMX: delete a setting from dashboard

All endpoints are JWT-protected. Superuser required for global/module writes.
"""

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import OwnerUser, get_current_user, redis_client
from app.core.templates import templates
from app.modules.settings import schemas, service
from app.modules.settings.schemas import (
    SettingBulkCreate,
    SettingCreate,
    SettingListResponse,
    SettingResolvedResponse,
    SettingResponse,
    SettingUpdate,
)

router = APIRouter(prefix="/settings", tags=["Settings"])


def _mask(resp: SettingResponse) -> SettingResponse:
    if resp.is_secret:
        resp.value = "••••••••"
    return resp


@router.get("/", response_model=SettingListResponse)
async def list_settings(
    scope: str | None = Query(None),
    module_name: str | None = Query(None),
    key_prefix: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = None if current_user.is_superuser else current_user.id
    items, total = await service.list_settings(
        db,
        scope=scope,
        module_name=module_name,
        user_id=uid,
        key_prefix=key_prefix,
        page=page,
        page_size=page_size,
    )
    return SettingListResponse(
        items=[_mask(SettingResponse.model_validate(s)) for s in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/", response_model=SettingResponse, status_code=201)
async def create_setting(
    body: SettingCreate,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.scope in (schemas.SettingScope.GLOBAL, schemas.SettingScope.MODULE):
        if not current_user.is_superuser:
            raise HTTPException(403, "Only superusers can modify global/module settings")
    uid = current_user.id if body.scope == schemas.SettingScope.USER else None
    setting = await service.upsert_setting(
        db,
        key=body.key,
        value=body.value,
        scope=body.scope.value,
        module_name=body.module_name,
        user_id=uid,
        description=body.description,
        value_type=body.value_type.value,
        is_secret=body.is_secret,
    )
    return _mask(SettingResponse.model_validate(setting))


@router.post("/bulk", response_model=list[SettingResponse], status_code=201)
async def bulk_create(
    body: SettingBulkCreate,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    for s in body.settings:
        if s.scope in (schemas.SettingScope.GLOBAL, schemas.SettingScope.MODULE):
            if not current_user.is_superuser:
                raise HTTPException(403, "Only superusers can modify global/module settings")
    data = [
        {
            "key": s.key,
            "value": s.value,
            "scope": s.scope.value,
            "module_name": s.module_name,
            "description": s.description,
            "value_type": s.value_type.value,
            "is_secret": s.is_secret,
        }
        for s in body.settings
    ]
    results = await service.bulk_upsert(db, data, user_id=current_user.id)
    return [_mask(SettingResponse.model_validate(s)) for s in results]


@router.get("/resolve", response_model=SettingResolvedResponse)
async def resolve_setting(
    key: str = Query(...),
    module_name: str | None = Query(None),
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    setting = await service.resolve_setting(
        db,
        key=key,
        module_name=module_name,
        user_id=current_user.id,
    )
    if not setting:
        raise HTTPException(404, f"Setting '{key}' not found at any scope")
    val = setting.value if not setting.is_secret else "••••••••"
    return SettingResolvedResponse(
        key=setting.key,
        value=val,
        value_type=setting.value_type,
        resolved_scope=setting.scope,
        source_id=setting.id,
    )


@router.get("/{setting_id}", response_model=SettingResponse)
async def get_setting(
    setting_id: int,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    setting = await service.get_setting_by_id(db, setting_id)
    if not setting:
        raise HTTPException(404, "Setting not found")
    if not current_user.is_superuser:
        if setting.scope == "user" and setting.user_id != current_user.id:
            raise HTTPException(403, "Access denied")
    return _mask(SettingResponse.model_validate(setting))


@router.patch("/{setting_id}", response_model=SettingResponse)
async def update_setting(
    setting_id: int,
    body: SettingUpdate,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    setting = await service.get_setting_by_id(db, setting_id)
    if not setting:
        raise HTTPException(404, "Setting not found")
    if setting.scope in ("global", "module") and not current_user.is_superuser:
        raise HTTPException(403, "Only superusers can modify global/module settings")
    if setting.scope == "user" and setting.user_id != current_user.id:
        raise HTTPException(403, "Cannot modify another user's settings")
    if body.value is not None:
        setting.value = body.value
    if body.description is not None:
        setting.description = body.description
    if body.value_type is not None:
        setting.value_type = body.value_type.value
    if body.is_secret is not None:
        setting.is_secret = body.is_secret
    await db.flush()
    await db.refresh(setting)
    return _mask(SettingResponse.model_validate(setting))


@router.delete("/{setting_id}", status_code=204)
async def delete_setting(
    setting_id: int,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    setting = await service.get_setting_by_id(db, setting_id)
    if not setting:
        raise HTTPException(404, "Setting not found")
    if setting.scope in ("global", "module") and not current_user.is_superuser:
        raise HTTPException(403, "Only superusers can delete global/module settings")
    if setting.scope == "user" and setting.user_id != current_user.id:
        raise HTTPException(403, "Cannot delete another user's settings")
    await service.delete_setting(db, setting_id)


@router.delete("/module/{module_name}", status_code=200)
async def delete_module_settings(
    module_name: str,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.is_superuser:
        raise HTTPException(403, "Only superusers can delete module settings")
    count = await service.delete_module_settings(db, module_name)
    return {"deleted": count, "module": module_name}


# ── Cookie-based auth helper for UI ──────────────────────
async def _get_user_from_cookie(request: Request, db: AsyncSession):
    """Extract owner from the access_token cookie (used by HTMX UI endpoints)."""
    session_id = request.cookies.get("access_token")
    if not session_id:
        return None
    if redis_client.get(f"session:{session_id}") == "1":
        return OwnerUser()
    return None


# ── HTMX UI Endpoints ────────────────────────────────────
async def _render_panel(request: Request, db: AsyncSession, user):
    """Helper to render the settings panel with current data."""
    if user:
        uid = None if user.is_superuser else user.id
        items, _ = await service.list_settings(db, user_id=uid, page=1, page_size=100)
    else:
        items = []
    return templates.TemplateResponse(request, "panel.html", {"settings": items})


@router.get("/ui/panel", include_in_schema=False)
async def ui_panel(request: Request, db: AsyncSession = Depends(get_db)):
    """Return the settings panel fragment for HTMX."""
    user = await _get_user_from_cookie(request, db)
    return await _render_panel(request, db, user)


@router.post("/ui/add", include_in_schema=False)
async def ui_add_setting(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    scope: str = Form("user"),
    db: AsyncSession = Depends(get_db),
):
    """Add a setting from the dashboard UI via HTMX."""
    user = await _get_user_from_cookie(request, db)
    if not user:
        return HTMLResponse(
            '<div class="p-3 text-xs font-mono text-red-500 border-2 border-red-500 rounded-none">AUTH REQUIRED</div>',
            status_code=401,
        )

    # Permission check for global/module scope
    if scope in ("global", "module") and not user.is_superuser:
        return HTMLResponse(
            '<div class="p-3 text-xs font-mono text-red-500 border-2 border-red-500 rounded-none">SUPERUSER REQUIRED FOR GLOBAL/MODULE SCOPE</div>',
            status_code=403,
        )

    uid = user.id if scope == "user" else None
    await service.upsert_setting(
        db,
        key=key,
        value=value,
        scope=scope,
        user_id=uid,
    )
    return await _render_panel(request, db, user)


@router.delete("/ui/{setting_id}", include_in_schema=False)
async def ui_delete_setting(
    setting_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Delete a setting from the dashboard UI via HTMX."""
    user = await _get_user_from_cookie(request, db)
    if not user:
        return HTMLResponse(
            '<div class="p-3 text-xs font-mono text-red-500 border-2 border-red-500 rounded-none">AUTH REQUIRED</div>',
            status_code=401,
        )

    setting = await service.get_setting_by_id(db, setting_id)
    if setting:
        if setting.scope in ("global", "module") and not user.is_superuser:
            return HTMLResponse(
                '<div class="p-3 text-xs font-mono text-red-500 border-2 border-red-500 rounded-none">PERMISSION DENIED</div>',
                status_code=403,
            )
        if setting.scope == "user" and setting.user_id != user.id:
            return HTMLResponse(
                '<div class="p-3 text-xs font-mono text-red-500 border-2 border-red-500 rounded-none">PERMISSION DENIED</div>',
                status_code=403,
            )
        await service.delete_setting(db, setting_id)

    return await _render_panel(request, db, user)
