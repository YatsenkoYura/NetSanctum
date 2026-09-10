from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr

from app.core.browser_client import browser_runtime_client
from app.core.browser_snapshots import browser_snapshot_store
from app.core.module_types import UI_EXTENSION_ID_PATTERN
from app.core.modules import module_registry
from app.core.security import get_current_user

router = APIRouter(prefix="/api/browser-runtime", tags=["browser-runtime"])


class BrowserStartRequest(BaseModel):
    policy_id: str = Field(min_length=1, max_length=128)
    locale: str = Field(default="en-US", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    restore_snapshot: bool = True
    mode: Literal["interactive", "headless"] = "interactive"


class BrowserClickRequest(BaseModel):
    x: float = Field(ge=0, le=1280)
    y: float = Field(ge=0, le=800)


class BrowserTypeRequest(BaseModel):
    text: SecretStr = Field(max_length=500)


class BrowserKeyRequest(BaseModel):
    key: str = Field(min_length=1, max_length=20)


class BrowserScrollRequest(BaseModel):
    delta_y: float = Field(ge=-2000, le=2000)


class BrowserNavigateRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4000)


class BrowserQueryRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


def _runtime_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=503, detail=str(exc))


@router.get("/policies")
async def list_browser_policies(user=Depends(get_current_user)):
    policies = module_registry.browser_policy_catalog()
    for policy in policies:
        policy["snapshot_exists"] = await browser_snapshot_store.exists(policy["id"])
    return policies


@router.post("/sessions")
async def start_browser_session(body: BrowserStartRequest, user=Depends(get_current_user)):
    resolved = module_registry.browser_policy(body.policy_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Browser policy is not active")
    record, policy = resolved
    try:
        return await browser_runtime_client.start(
            record.id,
            policy,
            locale=body.locale,
            restore_snapshot=body.restore_snapshot,
            mode=body.mode,
        )
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.get("/sessions/{session_id}")
async def browser_session_status(session_id: str, user=Depends(get_current_user)):
    try:
        return await browser_runtime_client.status(session_id)
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.get("/sessions/{session_id}/frame", include_in_schema=False)
async def browser_session_frame(session_id: str, user=Depends(get_current_user)):
    try:
        frame = await browser_runtime_client.screenshot(session_id)
    except Exception as exc:
        raise _runtime_error(exc) from exc
    return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/sessions/{session_id}/click")
async def browser_session_click(
    session_id: str,
    body: BrowserClickRequest,
    user=Depends(get_current_user),
):
    try:
        await browser_runtime_client.click(session_id, body.x, body.y)
        return {"status": "ok"}
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post("/sessions/{session_id}/type")
async def browser_session_type(
    session_id: str,
    body: BrowserTypeRequest,
    user=Depends(get_current_user),
):
    try:
        await browser_runtime_client.type_text(session_id, body.text.get_secret_value())
        return {"status": "ok"}
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post("/sessions/{session_id}/key")
async def browser_session_key(
    session_id: str,
    body: BrowserKeyRequest,
    user=Depends(get_current_user),
):
    try:
        await browser_runtime_client.press(session_id, body.key)
        return {"status": "ok"}
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post("/sessions/{session_id}/scroll")
async def browser_session_scroll(
    session_id: str,
    body: BrowserScrollRequest,
    user=Depends(get_current_user),
):
    try:
        await browser_runtime_client.scroll(session_id, body.delta_y)
        return {"status": "ok"}
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post("/sessions/{session_id}/query")
async def browser_session_query(
    session_id: str,
    body: BrowserQueryRequest,
    user=Depends(get_current_user),
):
    try:
        return {"items": await browser_runtime_client.query(session_id, body.selector, limit=body.limit)}
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post("/sessions/{session_id}/navigate")
async def browser_session_navigate(
    session_id: str,
    body: BrowserNavigateRequest,
    user=Depends(get_current_user),
):
    try:
        return await browser_runtime_client.navigate(session_id, body.url)
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.post("/sessions/{session_id}/snapshot")
async def save_browser_snapshot(session_id: str, user=Depends(get_current_user)):
    try:
        return await browser_runtime_client.save_snapshot(session_id)
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.delete("/sessions/{session_id}")
async def close_browser_session(session_id: str, user=Depends(get_current_user)):
    try:
        await browser_runtime_client.close(session_id)
        return {"status": "closed"}
    except Exception as exc:
        raise _runtime_error(exc) from exc


@router.delete("/snapshots/{policy_id}")
async def delete_browser_snapshot(policy_id: str, user=Depends(get_current_user)):
    if not UI_EXTENSION_ID_PATTERN.fullmatch(policy_id):
        raise HTTPException(status_code=400, detail="Invalid browser policy ID")
    await browser_runtime_client.delete_snapshot(policy_id)
    return {"status": "deleted"}
