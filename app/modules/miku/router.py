import asyncio
import base64
import binascii
import json
import logging
import re
import secrets
import time
from collections import deque
from datetime import UTC
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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.miku_memory_v1 import MikuMemorySearchRequest
from app.core.agent_client import AgentRuntimeUnavailableError
from app.core.database import AsyncSessionLocal, get_db
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
)
from app.core.modules import module_registry
from app.core.responses import serve_media_stream, serve_storage_file_chunked
from app.core.security import OwnerUser, get_current_user, redis_client
from app.core.templates import templates
from app.modules.miku.agent_turn import agent_client, runtime_capabilities
from app.modules.miku.cascades import list_cascades, undo_cascade_step
from app.modules.miku.conversations import (
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    list_messages,
    rename_conversation,
)
from app.modules.miku.integrations import search_memory
from app.modules.miku.models import MikuConversationNote
from app.modules.miku.providers import (
    load_provider_bundle,
    provider_settings_response,
    save_provider_settings,
)
from app.modules.miku.schemas import (
    MikuCapabilities,
    MikuConversationCreate,
    MikuConversationDetail,
    MikuConversationList,
    MikuConversationNoteItem,
    MikuConversationNoteList,
    MikuConversationRename,
    MikuConversationSummary,
    MikuConversationTurn,
    MikuJobStatus,
    MikuProviderSettingsResponse,
    MikuProviderSettingsUpdate,
    MikuQuery,
    MikuReference,
    MikuReply,
    MikuRuntimeCapabilities,
    MikuSocketMessage,
    MikuSpeechRequest,
    MikuStoredMessage,
)
from app.modules.miku.service import (
    MikuQueryError,
    MikuSessionContext,
    audit_turn,
    capabilities,
    job_status,
    query,
    resolve_resource,
)

router = APIRouter()
logger = logging.getLogger(__name__)
SOCKET_MESSAGE_LIMIT = 4096
SOCKET_VOICE_MESSAGE_LIMIT = 6_500_000
SOCKET_TURN_LIMIT = 20
SOCKET_TURN_WINDOW_SECONDS = 60
VOICE_AUDIO_LIMIT = 4 * 1024 * 1024
VOICE_AUDIO_TYPES = {"audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm"}
REST_CONTEXT_TTL_SECONDS = 900
REST_CONTEXT_LOCK_SECONDS = 300
SPEECH_CHUNK_LIMIT = 300


def _split_speech(text: str) -> list[str]:
    """Split reply text into speakable sentence chunks for streaming TTS.

    Each sentence becomes its own chunk so playback starts early; fragments
    shorter than 40 characters merge forward so the provider never gets
    one-word requests; oversized sentences hard-cut at the chunk limit.
    """
    sentences = [
        fragment.strip() for fragment in re.split(r"(?<=[.!?…\n])\s+", text.strip()) if fragment.strip()
    ]
    chunks: list[str] = []
    pending = ""
    for sentence in sentences:
        candidate = f"{pending} {sentence}".strip() if pending else sentence
        if len(candidate) < 40:
            pending = candidate
            continue
        if len(candidate) <= SPEECH_CHUNK_LIMIT:
            chunks.append(candidate)
            pending = ""
            continue
        if pending:
            chunks.append(pending)
            pending = ""
        while len(sentence) > SPEECH_CHUNK_LIMIT:
            chunks.append(sentence[:SPEECH_CHUNK_LIMIT])
            sentence = sentence[SPEECH_CHUNK_LIMIT:]
        if sentence:
            chunks.append(sentence)
    if pending:
        chunks.append(pending)
    return chunks


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
        payload = json.loads(raw)
        if isinstance(payload, list):
            references_payload = payload
            history_payload = []
        else:
            references_payload = payload.get("references", [])
            history_payload = payload.get("history", [])
        references = [MikuReference.model_validate(item) for item in references_payload]
        history = [MikuConversationTurn.model_validate(item) for item in history_payload]
    except (json.JSONDecodeError, TypeError, ValidationError):
        return MikuSessionContext()
    return MikuSessionContext(references=references[:20], history=history[-6:])


async def _save_rest_context(
    context_id: str | None, user_id: int, context: MikuSessionContext | None
) -> None:
    if not context_id or not context or (not context.references and not context.history):
        if context_id and context is not None:
            await redis_client.delete(f"miku:context:{user_id}:{context_id}")
        return
    await redis_client.setex(
        f"miku:context:{user_id}:{context_id}",
        REST_CONTEXT_TTL_SECONDS,
        json.dumps(
            {
                "references": [item.model_dump(mode="json") for item in (context.references or [])[:20]],
                "history": [item.model_dump(mode="json") for item in (context.history or [])[-6:]],
            }
        ),
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
    return await runtime_capabilities(agent_client(), providers)


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
    # Two turns in one thread must not interleave, so the lock is keyed by whichever
    # identity the request carries: a stored conversation, or the session context.
    lock_key = str(body.conversation_id) if body.conversation_id else body.context_id
    try:
        lock = await _acquire_context_lock(lock_key, user.id)
        context = await _rest_context(body.context_id, user.id)
        reply = await query(body, db, user, module_registry, context=context)
        if body.context_id and not body.conversation_id:
            await _save_rest_context(body.context_id, user.id, context)
        await db.commit()
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
        return {"text": await agent_client().transcribe(audio, content_type, provider=providers.stt)}
    except AgentRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/api/miku/speech")
async def miku_speech(
    body: MikuSpeechRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        providers = await load_provider_bundle(db, user.id)
        audio = await agent_client().synthesize(body.text, body.voice, provider=providers.tts)
    except AgentRuntimeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(audio.content, media_type=audio.media_type, headers={"Cache-Control": "no-store"})


@router.get("/api/miku/memory")
async def miku_memory(
    query: str | None = Query(default=None, max_length=200),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """What the assistant currently remembers, for the owner to see and correct."""
    request = MikuMemorySearchRequest(limit=20, **({"query": query} if query else {}))
    context = IntegrationContext(
        session=db,
        user=user,
        registry=module_registry,
        consumer_id="miku",
    )
    return await search_memory(request, context)


def _summary(conversation, message_count: int = 0) -> MikuConversationSummary:
    return MikuConversationSummary(
        id=conversation.id,
        title=conversation.title or "Новый диалог",
        message_count=message_count,
        created_at=_iso(conversation.created_at),
        updated_at=_iso(conversation.updated_at),
    )


def _iso(value) -> str:
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.isoformat()


@router.get("/api/miku/conversations", response_model=MikuConversationList)
async def miku_conversations(
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Every thread the owner has, most recently touched first."""
    rows = await list_conversations(db, user.id, limit=limit)
    return MikuConversationList(items=[_summary(conversation, count) for conversation, count in rows])


@router.post("/api/miku/conversations", response_model=MikuConversationSummary, status_code=201)
async def miku_conversation_create(
    body: MikuConversationCreate | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Start a new thread. The title is filled in from the first message."""
    conversation = await create_conversation(db, user.id, title=(body.title if body else ""))
    await db.commit()
    return _summary(conversation)


@router.get("/api/miku/conversations/{conversation_id}", response_model=MikuConversationDetail)
async def miku_conversation_detail(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """One thread with its full transcript, oldest message first."""
    conversation = await get_conversation(db, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    messages = await list_messages(db, conversation.id)
    return MikuConversationDetail(
        conversation=_summary(conversation, len(messages)),
        messages=[
            MikuStoredMessage(
                id=item.id,
                role=item.role,
                content=item.content,
                command=item.command,
                created_at=_iso(item.created_at),
            )
            for item in messages
        ],
    )


@router.put("/api/miku/conversations/{conversation_id}", response_model=MikuConversationSummary)
async def miku_conversation_rename(
    conversation_id: int,
    body: MikuConversationRename,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    conversation = await get_conversation(db, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    await rename_conversation(db, conversation, body.title)
    await db.commit()
    return _summary(conversation)


@router.delete("/api/miku/conversations/{conversation_id}", status_code=204)
async def miku_conversation_delete(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    conversation = await get_conversation(db, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    await delete_conversation(db, conversation)
    await db.commit()
    return Response(status_code=204)


@router.get("/api/miku/conversations/{conversation_id}/notes", response_model=MikuConversationNoteList)
async def miku_conversation_notes(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """What the agent decided to carry forward in this thread."""
    conversation = await get_conversation(db, user.id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="No such conversation")
    rows = await db.scalars(
        select(MikuConversationNote)
        .where(MikuConversationNote.conversation_id == conversation.id)
        .order_by(MikuConversationNote.id.desc())
    )
    return MikuConversationNoteList(
        items=[
            MikuConversationNoteItem(
                key=item.note_key,
                value=dict(item.value_json or {}),
                updated_at=_iso(item.updated_at),
            )
            for item in rows
        ]
    )


@router.get("/api/miku/cascades")
async def miku_cascades(
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """What the agent did recently, and which of its steps can be undone."""
    return await list_cascades(db, user, limit)


@router.post("/api/miku/cascades/{cascade_id}/undo/{step_index}")
async def miku_undo_cascade_step(
    cascade_id: int,
    step_index: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Reverse one recorded step through the undo integration its provider declared."""
    try:
        return await undo_cascade_step(db, user, cascade_id, step_index, module_registry)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IndexError as exc:
        raise HTTPException(status_code=422, detail="This step cannot be undone") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="The undo failed") from exc


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
    pending_turn: asyncio.Task | None = None
    pending_request_id: str | None = None
    pending_speech: asyncio.Task | None = None
    pending_speech_id: str | None = None

    async def _run_speech(request_id: str, text: str) -> None:
        try:
            speech_user = await websocket_owner_session(websocket)
            if not speech_user:
                await websocket.close(code=4401)
                return
        except Exception:
            await websocket.close(code=1011)
            return
        try:
            async with AsyncSessionLocal() as speech_db:
                providers = await load_provider_bundle(speech_db, speech_user.id)
                chunks = _split_speech(text)
                for index, chunk in enumerate(chunks):
                    audio = await agent_client().synthesize(chunk, "alloy", provider=providers.tts)
                    await _send_event(
                        websocket,
                        "speech.chunk",
                        request_id=request_id,
                        data={
                            "index": index,
                            "audio": base64.b64encode(audio.content).decode(),
                            "media_type": audio.media_type,
                            "final": index == len(chunks) - 1,
                        },
                    )
        except asyncio.CancelledError:
            await _send_event(websocket, "speech.cancelled", request_id=request_id)
            raise
        except AgentRuntimeUnavailableError:
            await _send_event(
                websocket,
                "speech.error",
                request_id=request_id,
                data={"code": "tts_unavailable"},
            )
        except Exception:
            logger.exception("MIKU speech streaming failed")
            await _send_event(
                websocket,
                "speech.error",
                request_id=request_id,
                data={"code": "tts_failed"},
            )

    async def _run_socket_turn(request_id: str, text: str, limit: int, conversation_id: int | None) -> None:
        async def _emit_turn_partial(phase: str, data: dict) -> None:
            await _send_event(websocket, "turn.partial", request_id=request_id, data={"phase": phase, **data})

        context_lock = None
        try:
            turn_user = await websocket_owner_session(websocket)
            if not turn_user:
                await websocket.close(code=4401)
                return
        except Exception:
            await websocket.close(code=1011)
            return
        await _send_event(websocket, "turn.started", request_id=request_id)
        turn_context = MikuSessionContext()
        try:
            lock_key = str(conversation_id) if conversation_id else context_id
            context_lock = await _acquire_context_lock(lock_key, turn_user.id)
            if context_id and not conversation_id:
                turn_context = await _rest_context(context_id, turn_user.id) or MikuSessionContext()
            async with AsyncSessionLocal() as db:
                reply = await query(
                    MikuQuery(message=text, limit=limit, conversation_id=conversation_id),
                    db,
                    user=turn_user,
                    registry=module_registry,
                    context=turn_context,
                    on_event=_emit_turn_partial,
                )
                if context_id and not conversation_id:
                    await _save_rest_context(context_id, turn_user.id, turn_context)
                try:
                    audit_turn(db, turn_user, request_id, "websocket", reply)
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("MIKU turn audit failed")
        except asyncio.CancelledError:
            await _send_event(websocket, "turn.cancelled", request_id=request_id)
            raise
        except MikuQueryError as exc:
            await _send_event(
                websocket,
                "turn.error",
                request_id=request_id,
                data={"code": "invalid_query", "message": str(exc)},
            )
            return
        except Exception:
            logger.exception("MIKU realtime turn failed")
            await _send_event(
                websocket,
                "turn.error",
                request_id=request_id,
                data={"code": "internal_error"},
            )
            return
        finally:
            await _release_context_lock(context_lock)
        await _send_event(
            websocket,
            "turn.result",
            request_id=request_id,
            data=reply.model_dump(mode="json"),
        )
        await _send_event(websocket, "turn.completed", request_id=request_id)

    def _clear_pending(task: asyncio.Task) -> None:
        nonlocal pending_turn, pending_request_id, pending_speech, pending_speech_id
        if pending_turn is task:
            pending_turn = None
            pending_request_id = None
        if pending_speech is task:
            pending_speech = None
            pending_speech_id = None

    def _cancel_all() -> None:
        if pending_turn is not None and not pending_turn.done():
            pending_turn.cancel()
        if pending_speech is not None and not pending_speech.done():
            pending_speech.cancel()

    async def _check_owner():
        try:
            owner = await websocket_owner_session(websocket)
        except Exception:
            await websocket.close(code=1011)
            return None
        if not owner:
            await websocket.close(code=4401)
            return None
        return owner

    def _check_rate_limit(request_id: str | None) -> bool:
        now = time.monotonic()
        while turn_times and now - turn_times[0] >= SOCKET_TURN_WINDOW_SECONDS:
            turn_times.popleft()
        if len(turn_times) >= SOCKET_TURN_LIMIT:
            return False
        turn_times.append(now)
        return True

    def _start_turn(request_id: str, text: str, limit: int, conversation_id: int | None = None) -> None:
        nonlocal pending_turn, pending_request_id
        # Barge-in: a new turn cancels the in-flight one and any speech.
        if pending_turn is not None and not pending_turn.done():
            pending_turn.cancel()
        if pending_speech is not None and not pending_speech.done():
            pending_speech.cancel()
        pending_request_id = request_id
        pending_turn = asyncio.create_task(_run_socket_turn(request_id, text, limit, conversation_id))
        pending_turn.add_done_callback(_clear_pending)

    try:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > SOCKET_VOICE_MESSAGE_LIMIT:
                await _send_event(websocket, "turn.error", data={"code": "message_too_large"})
                continue
            try:
                payload = json.loads(raw)
                message = MikuSocketMessage.model_validate(payload)
            except (json.JSONDecodeError, ValidationError):
                await _send_event(websocket, "turn.error", data={"code": "invalid_message"})
                continue
            if message.type != "voice" and len(raw) > SOCKET_MESSAGE_LIMIT:
                await _send_event(websocket, "turn.error", data={"code": "message_too_large"})
                continue

            if message.type == "ping":
                await _send_event(websocket, "session.pong", request_id=message.request_id)
                continue

            if message.type == "cancel":
                if pending_turn is not None and pending_request_id == message.request_id:
                    pending_turn.cancel()
                elif pending_speech is not None and pending_speech_id == message.request_id:
                    pending_speech.cancel()
                else:
                    await _send_event(websocket, "turn.cancelled", request_id=message.request_id)
                continue

            if not _check_rate_limit(message.request_id):
                await _send_event(
                    websocket,
                    "turn.error",
                    request_id=message.request_id,
                    data={"code": "rate_limited"},
                )
                continue

            user = await _check_owner()
            if user is None:
                return

            if message.type == "voice":
                content_type = (message.audio_content_type or "").partition(";")[0].lower()
                if content_type not in VOICE_AUDIO_TYPES:
                    await _send_event(
                        websocket,
                        "turn.error",
                        request_id=message.request_id,
                        data={"code": "unsupported_audio"},
                    )
                    continue
                try:
                    audio = base64.b64decode(message.audio or "", validate=True)
                except (binascii.Error, ValueError):
                    await _send_event(
                        websocket,
                        "turn.error",
                        request_id=message.request_id,
                        data={"code": "invalid_audio"},
                    )
                    continue
                if not audio or len(audio) > VOICE_AUDIO_LIMIT:
                    await _send_event(
                        websocket,
                        "turn.error",
                        request_id=message.request_id,
                        data={"code": "audio_too_large"},
                    )
                    continue
                try:
                    async with AsyncSessionLocal() as transcribe_db:
                        providers = await load_provider_bundle(transcribe_db, user.id)
                        text = await agent_client().transcribe(audio, content_type, provider=providers.stt)
                except AgentRuntimeUnavailableError:
                    await _send_event(
                        websocket,
                        "turn.error",
                        request_id=message.request_id,
                        data={"code": "stt_unavailable"},
                    )
                    continue
                except Exception:
                    logger.exception("MIKU voice transcription failed")
                    await _send_event(
                        websocket,
                        "turn.error",
                        request_id=message.request_id,
                        data={"code": "transcription_failed"},
                    )
                    continue
                if not text:
                    await _send_event(
                        websocket,
                        "turn.error",
                        request_id=message.request_id,
                        data={"code": "transcription_failed"},
                    )
                    continue
                await _send_event(
                    websocket,
                    "turn.partial",
                    request_id=message.request_id,
                    data={"phase": "transcript", "text": text},
                )
                _start_turn(message.request_id, text, message.limit, message.conversation_id)
                continue

            if message.type == "speak":
                if pending_speech is not None and not pending_speech.done():
                    pending_speech.cancel()
                pending_speech_id = message.request_id
                pending_speech = asyncio.create_task(_run_speech(message.request_id, message.message or ""))
                pending_speech.add_done_callback(_clear_pending)
                continue

            _start_turn(
                message.request_id,
                message.message or "",
                message.limit,
                message.conversation_id,
            )
    except WebSocketDisconnect:
        _cancel_all()
    finally:
        _cancel_all()
