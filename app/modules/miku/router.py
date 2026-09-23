import json
import logging
import re
import secrets
import time
from collections import deque
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.core.module_types import (
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationServiceError,
    IntegrationUnavailableError,
)
from app.core.modules import module_registry
from app.core.responses import serve_media_stream, serve_storage_file_chunked
from app.core.security import OwnerUser, get_current_user, redis_client
from app.core.templates import templates
from app.modules.miku.providers import (
    load_provider_bundle,
    provider_settings_response,
    save_provider_settings,
)
from app.modules.miku.runtime_client import MikuRuntimeUnavailableError, miku_runtime_client
from app.modules.miku.schemas import (
    MikuActionConfirmation,
    MikuActionResult,
    MikuCapabilities,
    MikuJobStatus,
    MikuProviderSettingsResponse,
    MikuProviderSettingsUpdate,
    MikuQuery,
    MikuReference,
    MikuReply,
    MikuRuntimeCapabilities,
    MikuSocketMessage,
    MikuSpeechRequest,
)
from app.modules.miku.service import (
    MikuQueryError,
    MikuSessionContext,
    audit_turn,
    capabilities,
    confirm_action,
    job_status,
    query,
    resolve_resource,
)

router = APIRouter()
logger = logging.getLogger(__name__)
SOCKET_MESSAGE_LIMIT = 4096
SOCKET_TURN_LIMIT = 20
SOCKET_TURN_WINDOW_SECONDS = 60
VOICE_AUDIO_LIMIT = 4 * 1024 * 1024
VOICE_AUDIO_TYPES = {"audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm"}
REST_CONTEXT_TTL_SECONDS = 900
REST_CONTEXT_LOCK_SECONDS = 180
RELEASE_CONTEXT_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def _bounded_body(request: Request, limit: int) -> bytes:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > limit:
            raise HTTPException(status_code=413, detail="Request body is too large")
    return bytes(content)


async def _rest_context(context_id: str | None, user_id: int) -> MikuSessionContext | None:
    if not context_id:
        return None
    raw = await redis_client.get(f"miku:context:{user_id}:{context_id}")
    if not raw:
        return MikuSessionContext()
    try:
        references = [MikuReference.model_validate(item) for item in json.loads(raw)]
    except (json.JSONDecodeError, TypeError, ValidationError):
        return MikuSessionContext()
    return MikuSessionContext(references=references[:20])


async def _save_rest_context(
    context_id: str | None, user_id: int, context: MikuSessionContext | None
) -> None:
    if not context_id or not context or not context.references:
        if context_id and context is not None:
            await redis_client.delete(f"miku:context:{user_id}:{context_id}")
        return
    await redis_client.setex(
        f"miku:context:{user_id}:{context_id}",
        REST_CONTEXT_TTL_SECONDS,
        json.dumps([item.model_dump(mode="json") for item in context.references[:20]]),
    )


async def _acquire_context_lock(context_id: str | None, user_id: int) -> tuple[str, str] | None:
    if not context_id:
        return None
    key = f"miku:context-lock:{user_id}:{context_id}"
    token = secrets.token_urlsafe(18)
    if not await redis_client.set(key, token, ex=REST_CONTEXT_LOCK_SECONDS, nx=True):
        raise MikuQueryError("Session context is busy; retry the request.")
    return key, token


async def _release_context_lock(lock: tuple[str, str] | None) -> None:
    if not lock:
        return
    key, token = lock
    try:
        await redis_client.eval(RELEASE_CONTEXT_LOCK_SCRIPT, 1, key, token)
    except Exception:
        logger.warning("MIKU context lock release failed", exc_info=True)


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
async def miku_dashboard(
    request: Request,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    providers = provider_settings_response(await load_provider_bundle(db, user.id))
    return templates.TemplateResponse(
        request,
        "miku_dashboard.html",
        {"user": user, "lang": request.cookies.get("lang", "en"), "providers": providers},
    )


@router.get("/api/miku/capabilities", response_model=MikuCapabilities)
async def miku_capabilities(user=Depends(get_current_user)):
    return capabilities(module_registry)


@router.get("/api/miku/runtime", response_model=MikuRuntimeCapabilities)
async def miku_runtime_capabilities(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    providers = await load_provider_bundle(db, user.id)
    return await miku_runtime_client.capabilities((providers.llm, providers.stt, providers.tts))


@router.put("/api/miku/providers", response_model=MikuProviderSettingsResponse)
async def miku_update_providers(
    body: MikuProviderSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    providers = await save_provider_settings(db, user.id, body)
    return provider_settings_response(providers)


@router.post("/api/miku/query", response_model=MikuReply)
async def miku_query(
    body: MikuQuery,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    lock = None
    try:
        lock = await _acquire_context_lock(body.context_id, user.id)
        context = await _rest_context(body.context_id, user.id)
        reply = await query(body, db, user, module_registry, context=context)
        await _save_rest_context(body.context_id, user.id, context)
        return reply
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await _release_context_lock(lock)


@router.post("/api/miku/transcribe")
async def miku_transcribe(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    content_type = request.headers.get("content-type", "").partition(";")[0].lower()
    if content_type not in VOICE_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid content length")
    if content_length > VOICE_AUDIO_LIMIT:
        raise HTTPException(status_code=413, detail="Audio utterance is too large")
    audio = await _bounded_body(request, VOICE_AUDIO_LIMIT)
    if not audio:
        raise HTTPException(status_code=413, detail="Audio utterance is empty")
    try:
        providers = await load_provider_bundle(db, user.id)
        return {"text": await miku_runtime_client.transcribe(audio, content_type, provider=providers.stt)}
    except MikuRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/miku/speech")
async def miku_speech(
    body: MikuSpeechRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        providers = await load_provider_bundle(db, user.id)
        audio = await miku_runtime_client.synthesize(body.text, body.voice, provider=providers.tts)
    except MikuRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(audio.content, media_type=audio.media_type, headers={"Cache-Control": "no-store"})


@router.post("/api/miku/actions/confirm", response_model=MikuActionResult)
async def miku_confirm_action(
    body: MikuActionConfirmation,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await confirm_action(body.confirmation_token, db, user, module_registry, redis_client)
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (IntegrationNotFoundError, IntegrationRejectedError, IntegrationUnavailableError) as exc:
        raise HTTPException(status_code=422, detail="The requested action is unavailable") from exc
    except IntegrationServiceError as exc:
        raise HTTPException(status_code=503, detail="The requested action failed") from exc


@router.get("/api/miku/jobs/{task_id}", response_model=MikuJobStatus)
async def miku_job_status(task_id: str, user=Depends(get_current_user)):
    try:
        status = await job_status(task_id)
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not status:
        raise HTTPException(status_code=404, detail="Job completed, expired, or unavailable")
    return status


@router.get("/api/miku/resources/{module_id}/{item_id}")
async def miku_resource(
    module_id: str,
    item_id: str,
    request: Request,
    child_id: str | None = None,
    page: int | None = Query(default=None, ge=0),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        resource = await resolve_resource(module_id, item_id, child_id, page, db, user, module_registry)
    except (MikuQueryError, IntegrationNotFoundError, IntegrationUnavailableError) as exc:
        raise HTTPException(status_code=404, detail="Resource is unavailable") from exc
    except (IntegrationRejectedError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Resource request was rejected") from exc
    if resource.kind == "text":
        return {"kind": "text", "title": resource.title, "text": resource.text or ""}
    if not resource.storage_path:
        raise HTTPException(status_code=404, detail="Resource file is unavailable")
    if resource.kind in {"audio", "video"}:
        return serve_media_stream(request, resource.storage_path)
    if resource.kind == "image":
        return serve_storage_file_chunked(resource.storage_path)
    raise HTTPException(status_code=422, detail="Unsupported resource type")


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
        data={"mode": "guarded", "protocol_version": 3},
    )
    turn_times: deque[float] = deque()
    context_id = websocket.query_params.get("context_id")
    if context_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", context_id):
        await websocket.close(code=4400)
        return
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
            context_lock = None
            try:
                context_lock = await _acquire_context_lock(context_id, user.id)
                if context_id:
                    context = await _rest_context(context_id, user.id) or MikuSessionContext()
                async with AsyncSessionLocal() as db:
                    reply = await query(
                        MikuQuery(message=message.message or "", limit=message.limit),
                        db,
                        user=user,
                        registry=module_registry,
                        context=context,
                    )
                    await _save_rest_context(context_id, user.id, context)
                    try:
                        audit_turn(db, user, message.request_id, "websocket", reply)
                        await db.commit()
                    except Exception:
                        await db.rollback()
                        logger.exception("MIKU turn audit failed")
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
            finally:
                await _release_context_lock(context_lock)
            await _send_event(
                websocket,
                "turn.result",
                request_id=message.request_id,
                data=reply.model_dump(mode="json"),
            )
            await _send_event(websocket, "turn.completed", request_id=message.request_id)
    except WebSocketDisconnect:
        pass
