import json
import logging
import time
from collections import deque
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.core.modules import module_registry
from app.core.security import OwnerUser, get_current_user, redis_client
from app.core.templates import templates
from app.modules.miku.schemas import MikuCapabilities, MikuQuery, MikuReply, MikuSocketMessage
from app.modules.miku.service import MikuQueryError, MikuSessionContext, capabilities, query

router = APIRouter()
logger = logging.getLogger(__name__)
SOCKET_MESSAGE_LIMIT = 4096
SOCKET_TURN_LIMIT = 20
SOCKET_TURN_WINDOW_SECONDS = 60


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    parsed = urlparse(origin)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == websocket.headers.get("host", "").lower()
    )


async def websocket_owner_session(websocket: WebSocket) -> OwnerUser | None:
    session_id = websocket.cookies.get("access_token")
    if session_id and await redis_client.get(f"session:{session_id}") == "1":
        return OwnerUser()
    return None


async def _send_event(
    websocket: WebSocket,
    event: str,
    *,
    request_id: str | None = None,
    data: dict | None = None,
) -> None:
    await websocket.send_json(
        {
            "event": event,
            "request_id": request_id,
            "data": data or {},
        }
    )


@router.get("/miku/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def miku_dashboard(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "miku_dashboard.html",
        {"user": user, "lang": request.cookies.get("lang", "en")},
    )


@router.get("/api/miku/capabilities", response_model=MikuCapabilities)
async def miku_capabilities(user=Depends(get_current_user)):
    return capabilities(module_registry)


@router.post("/api/miku/query", response_model=MikuReply)
async def miku_query(
    body: MikuQuery,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await query(body, db, user, module_registry)
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.websocket("/api/miku/ws")
async def miku_socket(websocket: WebSocket):
    if not websocket_origin_allowed(websocket):
        await websocket.close(code=4401)
        return
    try:
        user = await websocket_owner_session(websocket)
        if not user:
            await websocket.close(code=4401)
            return
    except Exception:
        await websocket.close(code=1011)
        return

    await websocket.accept()
    await _send_event(
        websocket,
        "session.ready",
        data={"mode": "read-only", "protocol_version": 1},
    )
    turn_times: deque[float] = deque()
    context = MikuSessionContext()
    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > SOCKET_MESSAGE_LIMIT:
                await _send_event(websocket, "turn.error", data={"code": "message_too_large"})
                continue
            try:
                message = MikuSocketMessage.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError):
                await _send_event(websocket, "turn.error", data={"code": "invalid_message"})
                continue

            if message.type == "ping":
                await _send_event(websocket, "session.pong", request_id=message.request_id)
                continue

            now = time.monotonic()
            while turn_times and now - turn_times[0] >= SOCKET_TURN_WINDOW_SECONDS:
                turn_times.popleft()
            if len(turn_times) >= SOCKET_TURN_LIMIT:
                await _send_event(
                    websocket,
                    "turn.error",
                    request_id=message.request_id,
                    data={"code": "rate_limited"},
                )
                continue
            turn_times.append(now)

            try:
                user = await websocket_owner_session(websocket)
                if not user:
                    await websocket.close(code=4401)
                    return
            except Exception:
                await websocket.close(code=1011)
                return

            await _send_event(websocket, "turn.started", request_id=message.request_id)
            try:
                async with AsyncSessionLocal() as db:
                    reply = await query(
                        MikuQuery(message=message.message or "", limit=message.limit),
                        db,
                        user=user,
                        registry=module_registry,
                        context=context,
                    )
            except MikuQueryError as exc:
                await _send_event(
                    websocket,
                    "turn.error",
                    request_id=message.request_id,
                    data={"code": "invalid_query", "message": str(exc)},
                )
                continue
            except Exception:
                logger.exception("MIKU realtime turn failed")
                await _send_event(
                    websocket,
                    "turn.error",
                    request_id=message.request_id,
                    data={"code": "internal_error"},
                )
                continue
            await _send_event(
                websocket,
                "turn.result",
                request_id=message.request_id,
                data=reply.model_dump(mode="json"),
            )
            await _send_event(websocket, "turn.completed", request_id=message.request_id)
    except WebSocketDisconnect:
        pass
