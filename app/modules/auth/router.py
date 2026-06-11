"""
Auth module — HTTP router for self-hosted single-user configuration.

Endpoints:
    POST /auth/login          — authenticate via JSON API using dynamic access token
    GET  /auth/me             — return static profile of the system owner
    GET  /auth/login-page     — render the single-field brutalist access terminal
    POST /auth/ui/login       — HTMX form handler for single access token login
    GET  /auth/logout-page    — clear session and redirect to login page
"""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.security import OwnerUser, get_current_user, redis_client, verify_access_token
from app.core.templates import templates

router = APIRouter(prefix="/auth", tags=["Auth"])


class TokenLoginRequest(BaseModel):
    token: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class OwnerResponse(BaseModel):
    id: int
    username: str
    email: str
    is_active: bool
    is_superuser: bool


# ── JSON API Endpoints ───────────────────────────────────
@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate using the access token and get JWT",
)
async def login(body: TokenLoginRequest):
    """API-based token verification."""
    if not verify_access_token(body.token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
        )

    return TokenResponse(access_token=body.token)


@router.get(
    "/me",
    response_model=OwnerResponse,
    summary="Get the active owner's profile",
)
async def me(current_user: OwnerUser = Depends(get_current_user)):
    """Return the statically defined OwnerUser context."""
    return OwnerResponse(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        is_active=current_user.is_active,
        is_superuser=current_user.is_superuser,
    )


# ── UI Page Endpoints ────────────────────────────────────
@router.get("/login-page", include_in_schema=False)
async def login_page(request: Request):
    """Render the simplified Neo-brutalist login terminal."""
    lang = request.cookies.get("lang") or "en"
    return templates.TemplateResponse(request, "login.html", {"user": None, "lang": lang})


@router.post("/ui/login", include_in_schema=False)
async def ui_login(
    request: Request,
    token: str = Form(...),
):
    """Handle the single access token form submission via HTMX."""
    lang = request.cookies.get("lang") or "en"
    if not verify_access_token(token.strip()):
        # Resolve dynamic translation for failure banner
        from app.core.i18n import translate

        err_msg = translate(request.scope, "auth", "invalid_token")
        return templates.TemplateResponse(
            request,
            "auth_error.html",
            {"error": err_msg, "lang": lang},
        )

    # Success: Generate session ID and store in Redis
    session_id = str(uuid.uuid4())
    redis_client.setex(f"session:{session_id}", 86400 * 7, "1")
    response = templates.TemplateResponse(request, "auth_success.html")
    response.set_cookie(
        key="access_token",
        value=session_id,
        httponly=True,
        samesite="lax",
        max_age=86400 * 7,  # 7-day session for convenient personal use
    )
    return response


@router.get("/logout-page", include_in_schema=False)
async def logout_page(request: Request):
    """Clear session cookie, remove from Redis, and redirect to login terminal."""
    session_id = request.cookies.get("access_token")
    if session_id:
        redis_client.delete(f"session:{session_id}")

    response = RedirectResponse(url="/auth/login-page", status_code=302)
    response.delete_cookie("access_token")
    return response
