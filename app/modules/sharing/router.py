import asyncio
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import compile_path

from app.core.config import get_settings
from app.core.database import get_db
from app.core.modules import module_registry
from app.core.security import (
    get_current_user,
    hash_password,
    redis_client,
    use_secure_cookies,
    verify_password,
)
from app.core.templates import templates
from app.modules.sharing.models import ShareLink
from app.modules.sharing.schemas import ShareCreate
from app.modules.sharing.service import (
    CREATE_SESSION_SCRIPT,
    MAX_SHARE_SESSIONS,
    RESERVE_PASSWORD_ATTEMPT_SCRIPT,
    hash_secret,
    is_active,
    session_ttl,
    utc_now,
    verify_secret,
)

router = APIRouter(tags=["sharing"])
logger = logging.getLogger(__name__)
settings = get_settings()
SHARE_COOKIE = "netsanctum_share"
PASSWORD_ATTEMPT_LIMIT = 5
PASSWORD_ATTEMPT_WINDOW = 300


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Shared content not found")


def _harden_shared_response(response: Response) -> Response:
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; media-src 'self'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return response


def _provider(module_id: str):
    provider = module_registry.share_provider(module_id)
    if provider is None:
        raise HTTPException(status_code=400, detail="Module does not support sharing")
    return provider


def _share_spec(module_id: str):
    spec = module_registry.share_spec(module_id)
    if spec is None:
        raise HTTPException(status_code=400, detail="Module does not support sharing")
    return spec


def _match_share_path(path: str, declarations: tuple) -> tuple[Any, dict] | None:
    candidate = f"/{path.strip('/')}"
    for declaration in declarations:
        regex, _format, converters = compile_path(f"/{declaration.path}")
        match = regex.fullmatch(candidate)
        if not match:
            continue
        params = {name: converters[name].convert(value) for name, value in match.groupdict().items()}
        return declaration, params
    return None


def _shared_response(payload) -> Response:
    if isinstance(payload, Response):
        return payload
    return JSONResponse(jsonable_encoder(payload))


async def _render_shared_application(request: Request, share: ShareLink) -> Response:
    spec = _share_spec(share.module_id)
    response = templates.TemplateResponse(
        request,
        spec.dashboard_template,
        {
            "module_base": "shared_base.html",
            "shared_mode": True,
            "share": share,
            "user": None,
            "lang": request.cookies.get("lang", "en"),
        },
    )
    response.body = response.body.replace(
        spec.api_prefix.encode(),
        f"/s/{share.id}{spec.api_prefix}".encode(),
    )
    response.headers["Content-Length"] = str(len(response.body))
    return response


async def _dispatch_shared_api(
    request: Request,
    share: ShareLink,
    db: AsyncSession,
    path: str,
) -> Response:
    if request.method not in {"GET", "HEAD"}:
        return JSONResponse({"detail": "Shared access is read-only"}, status_code=403)

    spec = _share_spec(share.module_id)
    provider = _provider(share.module_id)
    expected_prefix = spec.api_prefix.removeprefix("/api/")
    normalized = path.strip("/")
    if normalized == expected_prefix:
        relative_path = ""
    elif normalized.startswith(f"{expected_prefix}/"):
        relative_path = normalized.removeprefix(f"{expected_prefix}/")
    else:
        raise _not_found()

    matched_route = _match_share_path(relative_path, spec.routes)
    if matched_route:
        route, params = matched_route
        handler = getattr(provider, route.source, None)
        if handler is None:
            raise RuntimeError(f"Share provider has no {route.source!r} handler")
        return _shared_response(await handler(request, share, db, route, params))

    matched_asset = _match_share_path(relative_path, spec.assets)
    if matched_asset:
        asset, params = matched_asset
        handler = getattr(provider, "asset", None)
        if handler is None:
            raise RuntimeError("Share provider has no asset handler")
        return _shared_response(await handler(request, share, db, asset, params))

    raise _not_found()


def _summary(share: ShareLink) -> dict:
    effective_status = share.status
    if effective_status == "active" and not is_active(share):
        effective_status = "expired"
    return {
        "id": share.id,
        "module_id": share.module_id,
        "title": share.title,
        "selection_mode": share.selection_mode,
        "selector": share.selector,
        "public": share.is_public,
        "password_protected": bool(share.password_hash),
        "allow_download": share.allow_download,
        "status": effective_status,
        "expires_at": share.expires_at,
        "access_count": share.access_count,
        "last_accessed_at": share.last_accessed_at,
        "created_at": share.created_at,
        "entry_path": f"/s/{share.id}" if share.is_public else None,
    }


async def _active_share(db: AsyncSession, share_id: str) -> ShareLink:
    share = await db.get(ShareLink, share_id)
    if not share or not is_active(share) or module_registry.share_provider(share.module_id) is None:
        raise _not_found()
    return share


async def _has_session(request: Request, share_id: str) -> bool:
    session_id = request.cookies.get(SHARE_COOKIE)
    if not session_id:
        return False
    try:
        return await redis_client.get(f"share_session:{session_id}") == share_id
    except Exception:
        raise HTTPException(status_code=503, detail="Shared session service is unavailable")


async def _is_authorized(request: Request, share: ShareLink) -> bool:
    if share.is_public and not share.password_hash:
        return True
    return await _has_session(request, share.id)


async def _establish_session(request: Request, share: ShareLink, db: AsyncSession) -> Response:
    session_id = secrets.token_urlsafe(32)
    ttl = session_ttl(share)
    session_key = f"share_session:{session_id}"
    session_index = f"share_sessions:{share.id}"
    try:
        await redis_client.eval(
            CREATE_SESSION_SCRIPT,
            2,
            session_index,
            session_key,
            ttl,
            share.id,
            session_id,
            "share_session:",
            MAX_SHARE_SESSIONS,
        )
    except Exception:
        raise HTTPException(status_code=503, detail="Shared session service is unavailable")

    share.access_count += 1
    share.last_accessed_at = utc_now()
    await db.commit()

    response = RedirectResponse(url=f"/s/{share.id}", status_code=303)
    response.set_cookie(
        SHARE_COOKIE,
        session_id,
        httponly=True,
        secure=use_secure_cookies(request),
        samesite="lax",
        max_age=ttl,
        path=f"/s/{share.id}",
    )
    return _harden_shared_response(response)


def _unlock_page(
    request: Request,
    share: ShareLink,
    action: str,
    error: str | None = None,
    secret: str | None = None,
):
    response = templates.TemplateResponse(
        request,
        "share_unlock.html",
        {
            "share": share,
            "action": action,
            "error": error,
            "secret": secret,
            "lang": request.cookies.get("lang", "en"),
        },
        status_code=401 if error else 200,
    )
    return _harden_shared_response(response)


async def _reserve_password_attempt(request: Request, share_id: str) -> str:
    host = request.client.host if request.client else "unknown"
    key = f"share_attempts:{share_id}:{host}"
    try:
        allowed, retry_after = await redis_client.eval(
            RESERVE_PASSWORD_ATTEMPT_SCRIPT,
            1,
            key,
            PASSWORD_ATTEMPT_LIMIT,
            PASSWORD_ATTEMPT_WINDOW,
        )
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="Too many password attempts",
                headers={"Retry-After": str(max(1, int(retry_after)))},
            )
        return key
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Shared session service is unavailable")


async def _check_password(request: Request, share: ShareLink, password: str) -> bool:
    key = await _reserve_password_attempt(request, share.id)
    try:
        valid = bool(
            share.password_hash and await asyncio.to_thread(verify_password, password, share.password_hash)
        )
        if valid:
            await redis_client.delete(key)
        return valid
    except Exception:
        raise HTTPException(status_code=503, detail="Shared session service is unavailable")


@router.get("/shares/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def shares_dashboard(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "shares_dashboard.html",
        {"user": user, "lang": request.cookies.get("lang", "en")},
    )


@router.get("/api/shares/providers")
async def list_share_providers(user=Depends(get_current_user)):
    providers = []
    for record in module_registry.active_records():
        if not record.spec or not record.spec.share:
            continue
        provider = module_registry.share_provider(record.id)
        if provider is not None:
            providers.append(
                {
                    "module_id": record.id,
                    "title_en": record.spec.title_en,
                    "title_ru": record.spec.title_ru,
                    "selector_key": record.spec.share.selector_key,
                }
            )
    return providers


@router.get("/api/shares/providers/{module_id}/content")
async def list_shareable_content(
    module_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    return await _provider(module_id).catalog(db)


@router.get("/api/shares")
async def list_shares(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    result = await db.execute(select(ShareLink).order_by(ShareLink.created_at.desc()))
    return [_summary(share) for share in result.scalars().all()]


@router.post("/api/shares", status_code=201)
async def create_share(
    body: ShareCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    now = datetime.now(UTC)
    if body.expires_at is not None and body.expires_at <= now:
        raise HTTPException(status_code=422, detail="Expiration must be in the future")

    share_spec = _share_spec(body.module_id)
    selected_items = body.selector.get(share_spec.selector_key, [])
    if body.selection_mode == "selected" and (
        not isinstance(selected_items, list) or len(selected_items) > share_spec.max_items
    ):
        raise HTTPException(
            status_code=422,
            detail=f"A share may contain at most {share_spec.max_items} items",
        )
    selector = await _provider(body.module_id).selection(
        db,
        body.selection_mode,
        body.selector,
    )
    secret = None if body.public else secrets.token_urlsafe(32)
    share = ShareLink(
        id=str(uuid.uuid4()),
        module_id=body.module_id,
        title=body.title,
        selection_mode=body.selection_mode,
        selector=selector,
        is_public=body.public,
        secret_hash=hash_secret(secret) if secret else None,
        password_hash=hash_password(body.password) if body.password else None,
        allow_download=body.allow_download,
        status="active",
        expires_at=body.expires_at,
        access_count=0,
    )
    db.add(share)
    await db.commit()
    await db.refresh(share)

    base_url = settings.PUBLIC_BASE_URL.rstrip("/") or str(request.base_url).rstrip("/")
    path = f"/s/{share.id}" if share.is_public else f"/s/{share.id}#{secret}"
    return {**_summary(share), "url": f"{base_url}{path}"}


@router.delete("/api/shares/{share_id}")
async def revoke_share(
    share_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    share = await db.get(ShareLink, share_id)
    if not share:
        raise HTTPException(status_code=404, detail="Share not found")
    share.status = "revoked"
    share.revoked_at = utc_now()
    await db.commit()
    try:
        session_index = f"share_sessions:{share.id}"
        session_ids = await redis_client.zrange(session_index, 0, -1)
        if session_ids:
            await redis_client.delete(*(f"share_session:{session_id}" for session_id in session_ids))
        await redis_client.delete(session_index)
    except Exception as exc:
        logger.warning("Could not clear Redis sessions for revoked share %s: %s", share.id, exc)
    return {"status": "revoked", "id": share.id}


@router.post("/s/{share_id}/access", include_in_schema=False)
async def unlock_private_share(
    share_id: str,
    request: Request,
    secret: str = Form(...),
    password: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    share = await _active_share(db, share_id)
    if share.is_public or not verify_secret(secret, share.secret_hash):
        raise _not_found()
    if share.password_hash and not password:
        return _unlock_page(request, share, f"/s/{share.id}/access", secret=secret)
    if share.password_hash and not await _check_password(request, share, password or ""):
        return _unlock_page(
            request,
            share,
            f"/s/{share.id}/access",
            "Invalid password",
            secret,
        )
    return await _establish_session(request, share, db)


@router.post("/s/{share_id}/unlock", include_in_schema=False)
async def unlock_public_share(
    share_id: str,
    request: Request,
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    share = await _active_share(db, share_id)
    if not share.is_public or not share.password_hash:
        raise _not_found()
    if not await _check_password(request, share, password):
        return _unlock_page(request, share, f"/s/{share.id}/unlock", "Invalid password")
    return await _establish_session(request, share, db)


@router.api_route(
    "/s/{share_id}/api/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def shared_module_api(
    share_id: str,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    share = await _active_share(db, share_id)
    if not await _is_authorized(request, share):
        raise _not_found()
    response = await _dispatch_shared_api(request, share, db, path)
    return _harden_shared_response(response)


@router.get("/s/{share_id}", response_class=HTMLResponse, include_in_schema=False)
async def shared_application(
    share_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    share = await _active_share(db, share_id)
    if share.is_public and not share.password_hash:
        response = await _render_shared_application(request, share)
        return _harden_shared_response(response)

    if not await _has_session(request, share.id):
        if not share.is_public:
            response = templates.TemplateResponse(
                request,
                "share_bootstrap.html",
                {"share": share, "lang": request.cookies.get("lang", "en")},
            )
            return _harden_shared_response(response)
        if share.password_hash:
            return _unlock_page(request, share, f"/s/{share.id}/unlock")

    response = await _render_shared_application(request, share)
    return _harden_shared_response(response)
