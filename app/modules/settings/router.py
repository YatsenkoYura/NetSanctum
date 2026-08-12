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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.control_center import (
    cancel_tracked_task,
    clear_logs,
    format_timestamp,
    module_control_state,
    read_logs,
    runtime_overview,
    tasks_blocking_module_change,
    tracked_tasks,
)
from app.core.database import get_db
from app.core.module_config import reset_enabled_module_ids, save_enabled_module_ids
from app.core.modules import module_registry
from app.core.security import get_current_user
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


# ── HTMX UI Endpoints ────────────────────────────────────
async def _settings_context(db: AsyncSession, user) -> dict:
    uid = None if user.is_superuser else user.id
    items, _ = await service.list_settings(db, user_id=uid, page=1, page_size=200)
    return {"settings": items, "module_options": module_registry.records}


@router.get("/ui/control-center", include_in_schema=False)
async def control_center(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "control_center.html",
        {"user": user, "module_state": module_control_state()},
    )


@router.get("/ui/overview", include_in_schema=False)
async def ui_overview(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "control_overview.html",
        {"overview": await runtime_overview()},
    )


@router.get("/ui/tasks", include_in_schema=False)
async def ui_tasks(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(request, "control_tasks.html", {"tasks": await tracked_tasks()})


@router.delete("/ui/tasks/{task_id}", include_in_schema=False)
async def ui_cancel_task(task_id: str, request: Request, user=Depends(get_current_user)):
    try:
        await cancel_tracked_task(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return templates.TemplateResponse(request, "control_tasks.html", {"tasks": await tracked_tasks()})


@router.get("/ui/logs", include_in_schema=False)
async def ui_logs(
    request: Request,
    level: str | None = Query(None),
    role: str | None = Query(None),
    query: str | None = Query(None, max_length=100),
    user=Depends(get_current_user),
):
    logs = await read_logs(limit=250, level=level, role=role, query=query)
    return templates.TemplateResponse(
        request,
        "control_logs.html",
        {
            "logs": logs,
            "selected_level": level or "",
            "selected_role": role or "",
            "query": query or "",
            "format_timestamp": format_timestamp,
        },
    )


@router.delete("/ui/logs", include_in_schema=False)
async def ui_clear_logs(request: Request, user=Depends(get_current_user)):
    await clear_logs()
    return templates.TemplateResponse(
        request,
        "control_logs.html",
        {
            "logs": [],
            "selected_level": "",
            "selected_role": "",
            "query": "",
            "format_timestamp": format_timestamp,
        },
    )


@router.get("/ui/modules", include_in_schema=False)
async def ui_modules(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request, "control_modules.html", {"module_state": module_control_state()}
    )


@router.post("/ui/modules", include_in_schema=False)
async def ui_save_modules(
    request: Request,
    enabled_modules: list[str] = Form(default=[]),
    user=Depends(get_current_user),
):
    selectable = {
        record.id
        for record in module_registry.records
        if record.spec and not record.spec.required and module_registry.is_installed(record.id)
    }
    selected = set(enabled_modules)
    if not selected <= selectable:
        raise HTTPException(400, "Requested module is not installed or cannot be configured")
    blocked_modules = tasks_blocking_module_change(selected, await tracked_tasks())
    if blocked_modules:
        return templates.TemplateResponse(
            request,
            "control_modules.html",
            {
                "module_state": module_control_state(),
                "save_error": "Stop active tasks before disabling: " + ", ".join(sorted(blocked_modules)),
            },
        )
    try:
        save_enabled_module_ids(selected)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return templates.TemplateResponse(
        request,
        "control_modules.html",
        {"module_state": module_control_state(), "saved": True},
    )


@router.delete("/ui/modules", include_in_schema=False)
async def ui_reset_modules(request: Request, user=Depends(get_current_user)):
    try:
        reset_enabled_module_ids()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return templates.TemplateResponse(
        request,
        "control_modules.html",
        {"module_state": module_control_state(), "saved": True},
    )


@router.get("/control/snapshot")
async def control_snapshot(user=Depends(get_current_user)):
    return {
        "overview": await runtime_overview(),
        "modules": module_control_state(),
        "tasks": await tracked_tasks(),
    }


@router.get("/ui/panel", include_in_schema=False)
async def ui_panel(
    request: Request,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return templates.TemplateResponse(request, "control_settings.html", await _settings_context(db, user))


@router.post("/ui/add", include_in_schema=False)
async def ui_add_setting(
    request: Request,
    key: str = Form(...),
    value: str = Form(...),
    scope: str = Form("user"),
    module_name: str | None = Form(None),
    description: str | None = Form(None),
    value_type: str = Form("string"),
    is_secret: bool = Form(False),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if scope not in {item.value for item in schemas.SettingScope}:
        raise HTTPException(400, "Invalid setting scope")
    if value_type not in {item.value for item in schemas.ValueType}:
        raise HTTPException(400, "Invalid value type")
    if scope in ("global", "module") and not user.is_superuser:
        raise HTTPException(403, "Superuser required")
    if scope == "module" and (not module_name or not module_registry.is_installed(module_name)):
        raise HTTPException(400, "Module scope requires an installed module")

    uid = user.id if scope == "user" else None
    await service.upsert_setting(
        db,
        key=key,
        value=value,
        scope=scope,
        module_name=module_name if scope == "module" else None,
        user_id=uid,
        description=description,
        value_type=value_type,
        is_secret=is_secret,
    )
    return templates.TemplateResponse(request, "control_settings.html", await _settings_context(db, user))


@router.delete("/ui/settings/{setting_id}", include_in_schema=False)
async def ui_delete_setting(
    setting_id: int,
    request: Request,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    setting = await service.get_setting_by_id(db, setting_id)
    if setting:
        if setting.scope in ("global", "module") and not user.is_superuser:
            raise HTTPException(403, "Permission denied")
        if setting.scope == "user" and setting.user_id != user.id:
            raise HTTPException(403, "Permission denied")
        await service.delete_setting(db, setting_id)

    return templates.TemplateResponse(request, "control_settings.html", await _settings_context(db, user))
