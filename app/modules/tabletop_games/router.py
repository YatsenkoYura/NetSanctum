import io
import logging
from datetime import UTC, datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.realtime import realtime_hub
from app.core.security import get_current_user, redis_client, use_secure_cookies
from app.core.templates import templates
from app.modules.tabletop_games.models import TabletopMessage, TabletopParticipant, TabletopRoom
from app.modules.tabletop_games.registry import game_registry
from app.modules.tabletop_games.schemas import (
    MessageCreate,
    ParticipantEffect,
    ParticipantSwap,
    ParticipantUpdate,
)
from app.modules.tabletop_games.services import (
    authenticate_player,
    close_room,
    create_room,
    get_room,
    get_room_by_code,
    join_room,
    list_rooms,
    owner_state,
    player_cookie_name,
    player_state,
    role_payload,
    room_channel,
    room_payload,
    set_participant_role,
    start_room,
    swap_participants,
)

router = APIRouter(tags=["tabletop-games"])
settings = get_settings()
logger = logging.getLogger(__name__)
GRIMOIRE_EFFECTS = (
    "Отравлен",
    "Пьян",
    "Защищён",
    "Без способности",
    "Безумие",
    "Под угрозой",
)


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Игровая комната не найдена")


def room_scenario(room: TabletopRoom) -> dict[str, str]:
    game = game_registry.get(room.game_id)
    scenarios = game.metadata.get("scenarios", ()) if game else ()
    return next(
        (scenario for scenario in scenarios if scenario["id"] == room.config.get("script")),
        {"title": str(room.config.get("script", "Сценарий"))},
    )


async def notify(room_id: str, event: str) -> None:
    try:
        await realtime_hub.publish(room_channel(room_id), event)
    except Exception:
        logger.exception("Could not publish tabletop event %s", event)


async def require_room(db: AsyncSession, room_id: str) -> TabletopRoom:
    room = await get_room(db, room_id)
    if not room:
        raise not_found()
    return room


async def require_public_room(db: AsyncSession, code: str) -> TabletopRoom:
    room = await get_room_by_code(db, code)
    if not room:
        raise not_found()
    return room


async def require_player(request: Request, db: AsyncSession, room: TabletopRoom) -> TabletopParticipant:
    participant = await authenticate_player(db, room, request.cookies.get(player_cookie_name(room.code)))
    if not participant:
        raise HTTPException(status_code=401, detail="Сессия игрока недействительна")
    return participant


async def reserve_message(request: Request, room_id: str, participant_id: str) -> None:
    host = request.client.host if request.client else "unknown"
    key = f"tabletop:message-limit:{room_id}:{participant_id}:{host}"
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 10)
        if count > 12:
            raise HTTPException(status_code=429, detail="Слишком много сообщений")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Сервис игровых сессий недоступен")


def websocket_origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return False
    parsed = urlparse(origin)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == websocket.headers.get("host", "").lower()
    )


async def websocket_owner(websocket: WebSocket) -> bool:
    session_id = websocket.cookies.get("access_token")
    return bool(session_id and await redis_client.get(f"session:{session_id}") == "1")


@router.get("/tabletop", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
        "tabletop_dashboard.html",
        {
            "user": user,
            "lang": request.cookies.get("lang", "ru"),
            "games": game_registry.all(),
            "rooms": await list_rooms(db),
        },
    )


@router.get("/tabletop/games/{game_id}", response_class=HTMLResponse, include_in_schema=False)
async def game_setup(request: Request, game_id: str, user=Depends(get_current_user)):
    game = game_registry.get(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Игра не установлена")
    return templates.TemplateResponse(
        request,
        "tabletop_setup.html",
        {"user": user, "lang": request.cookies.get("lang", "ru"), "game": game},
    )


@router.post("/tabletop/rooms", include_in_schema=False)
async def create_game_room(
    request: Request,
    game_id: str = Form(...),
    title: str = Form(..., min_length=1, max_length=120),
    player_limit: int = Form(10),
    script: str = Form("trouble_brewing"),
    player_chat: str = Form("private"),
    evil_info: str = Form("standard"),
    demon_bluff_count: int = Form(3),
    minion_bluffs: bool = Form(False),
    evil_code_word: str = Form(""),
    player_information: str = Form("full"),
    manual_distribution: bool = Form(False),
    townsfolk_count: int | None = Form(None),
    outsider_count: int | None = Form(None),
    minion_count: int | None = Form(None),
    demon_count: int | None = Form(None),
    show_online_status: bool = Form(False),
    reveal_roles_on_end: bool = Form(False),
    show_storyteller_reference: bool = Form(False),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    game = game_registry.get(game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Игра не установлена")
    try:
        room = await create_room(
            db,
            game,
            title.strip(),
            {
                "player_limit": player_limit,
                "script": script,
                "player_chat": player_chat,
                "evil_info": evil_info,
                "demon_bluff_count": demon_bluff_count,
                "minion_bluffs": minion_bluffs,
                "evil_code_word": evil_code_word,
                "player_information": player_information,
                "manual_distribution": manual_distribution,
                "townsfolk_count": townsfolk_count,
                "outsider_count": outsider_count,
                "minion_count": minion_count,
                "demon_count": demon_count,
                "show_online_status": show_online_status,
                "reveal_roles_on_end": reveal_roles_on_end,
                "show_storyteller_reference": show_storyteller_reference,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return RedirectResponse(f"/tabletop/rooms/{room.id}", status_code=303)


@router.get("/tabletop/rooms/{room_id}", response_class=HTMLResponse, include_in_schema=False)
async def host_room(
    request: Request,
    room_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    room = await require_room(db, room_id)
    game = game_registry.get(room.game_id)
    if not game:
        raise HTTPException(status_code=404, detail="Игра не установлена")
    base_url = settings.PUBLIC_BASE_URL.rstrip("/") or str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        request,
        "tabletop_host.html",
        {
            "user": user,
            "lang": request.cookies.get("lang", "ru"),
            "room": room,
            "game": game,
            "scenario": room_scenario(room),
            "join_url": f"{base_url}/tabletop/join/{room.code}",
            "role_options": [
                role_payload(role)
                for role in game.role_catalog
                if role.id in game.metadata["script_role_ids"][room.config["script"]]
            ],
            "grimoire_effects": GRIMOIRE_EFFECTS,
        },
    )


@router.get("/tabletop/rooms/{room_id}/qr.svg", include_in_schema=False)
async def room_qr(
    request: Request,
    room_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    import qrcode
    import qrcode.image.svg

    room = await require_room(db, room_id)
    base_url = settings.PUBLIC_BASE_URL.rstrip("/") or str(request.base_url).rstrip("/")
    image = qrcode.make(
        f"{base_url}/tabletop/join/{room.code}",
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=8,
        border=2,
    )
    output = io.BytesIO()
    image.save(output)
    return Response(
        output.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/tabletop/rooms/{room_id}")
async def get_owner_room_state(
    room_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    return await owner_state(db, await require_room(db, room_id))


@router.post("/api/tabletop/rooms/{room_id}/start")
async def start_game(
    room_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    room = await require_room(db, room_id)
    try:
        await start_room(db, room)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await notify(room.id, "game.started")
    return room_payload(room)


@router.post("/api/tabletop/rooms/{room_id}/end")
async def end_game(
    room_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    room = await require_room(db, room_id)
    room.status = "ended"
    room.ended_at = datetime.now(UTC)
    await db.commit()
    await notify(room.id, "game.ended")
    return room_payload(room)


@router.post("/api/tabletop/rooms/{room_id}/close")
async def force_close_game(
    room_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    room = await close_room(db, await require_room(db, room_id))
    await notify(room.id, "game.closed")
    return room_payload(room)


@router.patch("/api/tabletop/rooms/{room_id}/participants/{participant_id}")
async def update_participant(
    room_id: str,
    participant_id: str,
    body: ParticipantUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    room = await require_room(db, room_id)
    participant = await db.scalar(
        select(TabletopParticipant).where(
            TabletopParticipant.id == participant_id, TabletopParticipant.room_id == room.id
        )
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    values = body.model_dump(exclude_unset=True)
    role_id = values.pop("role_id", None)
    if role_id is not None:
        try:
            await set_participant_role(db, room, participant, role_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if "reminders" in values:
        values["reminders"] = [item.strip()[:80] for item in values["reminders"] if item.strip()]
    for field, value in values.items():
        setattr(participant, field, value)
    if values:
        await db.commit()
    await notify(room.id, "grimoire.updated")
    return {"status": "ok"}


@router.post("/api/tabletop/rooms/{room_id}/participants/{participant_id}/effects")
async def add_participant_effect(
    room_id: str,
    participant_id: str,
    body: ParticipantEffect,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if body.effect not in GRIMOIRE_EFFECTS:
        raise HTTPException(status_code=422, detail="Неизвестный эффект гримуара")
    room = await require_room(db, room_id)
    participant = await db.scalar(
        select(TabletopParticipant).where(
            TabletopParticipant.id == participant_id, TabletopParticipant.room_id == room.id
        )
    )
    if not participant:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    if body.effect not in participant.reminders:
        participant.reminders = [*participant.reminders, body.effect]
        await db.commit()
        await notify(room.id, "grimoire.updated")
    return {"status": "ok"}


@router.post("/api/tabletop/rooms/{room_id}/participants/{participant_id}/swap")
async def swap_player_seats(
    room_id: str,
    participant_id: str,
    body: ParticipantSwap,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    room = await require_room(db, room_id)
    try:
        await swap_participants(db, room, participant_id, body.target_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    await notify(room.id, "grimoire.reordered")
    return {"status": "ok"}


@router.post("/api/tabletop/rooms/{room_id}/messages", status_code=201)
async def owner_message(
    room_id: str,
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    room = await require_room(db, room_id)
    audience = "broadcast" if body.audience == "broadcast" else "player"
    recipient = None
    if audience == "player":
        recipient = await db.scalar(
            select(TabletopParticipant).where(
                TabletopParticipant.id == body.recipient_id,
                TabletopParticipant.room_id == room.id,
            )
        )
        if not recipient:
            raise HTTPException(status_code=422, detail="Выберите получателя")
    message = TabletopMessage(
        room_id=room.id,
        sender_participant_id=None,
        recipient_participant_id=recipient.id if recipient else None,
        audience=audience,
        text=body.text.strip(),
    )
    db.add(message)
    await db.commit()
    await notify(room.id, "message.created")
    return {"id": message.id}


@router.get("/tabletop/join/{code}", response_class=HTMLResponse, include_in_schema=False)
async def join_page(request: Request, code: str, db: AsyncSession = Depends(get_db)):
    room = await require_public_room(db, code)
    if room.status == "closed":
        raise HTTPException(status_code=410, detail="Комната закрыта ведущим")
    participant = await authenticate_player(db, room, request.cookies.get(player_cookie_name(room.code)))
    if participant:
        return RedirectResponse(f"/tabletop/room/{room.code}", status_code=302)
    return templates.TemplateResponse(
        request,
        "tabletop_join.html",
        {
            "user": None,
            "lang": request.cookies.get("lang", "ru"),
            "room": room,
            "game": game_registry.get(room.game_id),
            "scenario": room_scenario(room),
            "error": None,
        },
    )


@router.post("/tabletop/join/{code}", response_class=HTMLResponse, include_in_schema=False)
async def join_game(
    request: Request,
    code: str,
    nickname: str = Form(..., min_length=1, max_length=40),
    db: AsyncSession = Depends(get_db),
):
    room = await require_public_room(db, code)
    if room.status == "closed":
        raise HTTPException(status_code=410, detail="Комната закрыта ведущим")
    existing_participant = await authenticate_player(
        db, room, request.cookies.get(player_cookie_name(room.code))
    )
    if existing_participant:
        return RedirectResponse(f"/tabletop/room/{room.code}", status_code=303)
    nickname = " ".join(nickname.strip().split())
    if not nickname:
        raise HTTPException(status_code=422, detail="Введите ник")
    try:
        _participant, token = await join_room(db, room, nickname)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "tabletop_join.html",
            {
                "user": None,
                "lang": request.cookies.get("lang", "ru"),
                "room": room,
                "game": game_registry.get(room.game_id),
                "scenario": room_scenario(room),
                "error": str(exc),
            },
            status_code=409,
        )
    response = RedirectResponse(f"/tabletop/room/{room.code}", status_code=303)
    response.set_cookie(
        player_cookie_name(room.code),
        token,
        httponly=True,
        secure=use_secure_cookies(request),
        samesite="lax",
        max_age=86400,
        # A room-scoped cookie is available on its join URL so one browser profile
        # cannot silently overwrite its current player identity for that room.
        path="/tabletop",
    )
    await notify(room.id, "player.joined")
    return response


@router.get("/tabletop/room/{code}", response_class=HTMLResponse, include_in_schema=False)
async def player_room(request: Request, code: str, db: AsyncSession = Depends(get_db)):
    room = await require_public_room(db, code)
    if room.status == "closed":
        raise HTTPException(status_code=410, detail="Комната закрыта ведущим")
    participant = await require_player(request, db, room)
    response = templates.TemplateResponse(
        request,
        "tabletop_player.html",
        {
            "user": None,
            "lang": request.cookies.get("lang", "ru"),
            "room": room,
            "participant": participant,
            "game": game_registry.get(room.game_id),
            "scenario": room_scenario(room),
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("/tabletop/room/{code}/api/state")
async def get_player_state(
    request: Request,
    code: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    room = await require_public_room(db, code)
    if room.status == "closed":
        raise HTTPException(status_code=410, detail="Комната закрыта ведущим")
    response.headers["Cache-Control"] = "private, no-store"
    return await player_state(db, room, await require_player(request, db, room))


@router.post("/tabletop/room/{code}/api/messages", status_code=201)
async def player_message(
    request: Request,
    code: str,
    body: MessageCreate,
    db: AsyncSession = Depends(get_db),
):
    room = await require_public_room(db, code)
    if room.status == "closed":
        raise HTTPException(status_code=410, detail="Комната закрыта ведущим")
    participant = await require_player(request, db, room)
    await reserve_message(request, room.id, participant.id)
    if room.status == "ended":
        raise HTTPException(status_code=409, detail="Игра завершена")
    chat_mode = room.config.get(
        "player_chat", "private" if room.config.get("allow_player_messages", True) else "gm_only"
    )
    if chat_mode == "off":
        raise HTTPException(status_code=403, detail="Отправка сообщений отключена")
    audience = "gm" if body.audience == "gm" else "player"
    recipient = None
    if audience == "player":
        if chat_mode != "private":
            raise HTTPException(status_code=403, detail="Личные сообщения игроков отключены")
        recipient = await db.scalar(
            select(TabletopParticipant).where(
                TabletopParticipant.id == body.recipient_id,
                TabletopParticipant.room_id == room.id,
                TabletopParticipant.id != participant.id,
            )
        )
        if not recipient:
            raise HTTPException(status_code=422, detail="Выберите получателя")
    message = TabletopMessage(
        room_id=room.id,
        sender_participant_id=participant.id,
        recipient_participant_id=recipient.id if recipient else None,
        audience=audience,
        text=body.text.strip(),
    )
    db.add(message)
    await db.commit()
    await notify(room.id, "message.created")
    return {"id": message.id}


@router.websocket("/tabletop/rooms/{room_id}/ws")
async def owner_socket(websocket: WebSocket, room_id: str):
    if not websocket_origin_allowed(websocket) or not await websocket_owner(websocket):
        await websocket.close(code=4401)
        return
    async with AsyncSessionLocal() as db:
        room = await get_room(db, room_id)
    if not room:
        await websocket.close(code=4404)
        return
    channel = room_channel(room.id)
    await websocket.accept()
    await realtime_hub.connect(channel, websocket)
    await websocket.send_json({"event": "connected", "data": {}})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await realtime_hub.disconnect(channel, websocket)


@router.websocket("/tabletop/room/{code}/ws")
async def player_socket(websocket: WebSocket, code: str):
    if not websocket_origin_allowed(websocket):
        await websocket.close(code=4401)
        return
    async with AsyncSessionLocal() as db:
        room = await get_room_by_code(db, code)
        participant = (
            await authenticate_player(db, room, websocket.cookies.get(player_cookie_name(room.code)))
            if room
            else None
        )
    if not room or not participant:
        await websocket.close(code=4401)
        return
    channel = room_channel(room.id)
    presence_key = f"tabletop:presence:{room.id}:{participant.id}"
    await websocket.accept()
    await realtime_hub.connect(channel, websocket)
    await redis_client.setex(presence_key, 45, "1")
    await notify(room.id, "presence.changed")
    await websocket.send_json({"event": "connected", "data": {}})
    try:
        while True:
            await websocket.receive_text()
            await redis_client.setex(presence_key, 45, "1")
    except WebSocketDisconnect:
        pass
    finally:
        await redis_client.delete(presence_key)
        await realtime_hub.disconnect(channel, websocket)
        await notify(room.id, "presence.changed")
