import hashlib
import secrets
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import redis_client
from app.modules.tabletop_games.domain import GameDefinition, RoleDefinition
from app.modules.tabletop_games.models import TabletopMessage, TabletopParticipant, TabletopRoom
from app.modules.tabletop_games.registry import game_registry

ROOM_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def hash_player_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def player_cookie_name(room_code: str) -> str:
    return f"tabletop_{room_code.lower()}"


def room_channel(room_id: str) -> str:
    return f"tabletop:room:{room_id}"


def role_payload(role: RoleDefinition) -> dict[str, str]:
    return asdict(role)


async def create_room(
    db: AsyncSession,
    game: GameDefinition,
    title: str,
    config: dict[str, Any],
) -> TabletopRoom:
    title = " ".join(title.split())
    if not title:
        raise ValueError("Введите название партии")
    for _attempt in range(10):
        code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(8))
        exists = await db.scalar(select(TabletopRoom.id).where(TabletopRoom.code == code))
        if not exists:
            break
    else:
        raise RuntimeError("Could not allocate a room code")
    room = TabletopRoom(
        id=str(uuid.uuid4()),
        code=code,
        game_id=game.id,
        title=title,
        config=game.validate_config(config),
        status="lobby",
        game_state={},
    )
    db.add(room)
    await db.commit()
    await db.refresh(room)
    return room


async def list_rooms(db: AsyncSession) -> list[TabletopRoom]:
    result = await db.execute(
        select(TabletopRoom)
        .options(selectinload(TabletopRoom.participants))
        .order_by(TabletopRoom.created_at.desc())
        .limit(50)
    )
    return list(result.scalars().unique())


async def get_room_by_code(
    db: AsyncSession, code: str, *, with_participants: bool = False
) -> TabletopRoom | None:
    query = select(TabletopRoom).where(TabletopRoom.code == code.upper())
    if with_participants:
        query = query.options(selectinload(TabletopRoom.participants))
    return (await db.execute(query)).scalar_one_or_none()


async def get_room(db: AsyncSession, room_id: str, *, with_participants: bool = False) -> TabletopRoom | None:
    query = select(TabletopRoom).where(TabletopRoom.id == room_id)
    if with_participants:
        query = query.options(selectinload(TabletopRoom.participants))
    return (await db.execute(query)).scalar_one_or_none()


async def join_room(db: AsyncSession, room: TabletopRoom, nickname: str) -> tuple[TabletopParticipant, str]:
    await db.execute(select(TabletopRoom.id).where(TabletopRoom.id == room.id).with_for_update())
    if room.status != "lobby":
        raise ValueError("Игра уже началась")
    count = int(
        await db.scalar(
            select(func.count(TabletopParticipant.id)).where(TabletopParticipant.room_id == room.id)
        )
        or 0
    )
    if count >= int(room.config["player_limit"]):
        raise ValueError("Комната заполнена")
    duplicate = await db.scalar(
        select(TabletopParticipant.id).where(
            TabletopParticipant.room_id == room.id,
            func.lower(TabletopParticipant.nickname) == nickname.lower(),
        )
    )
    if duplicate:
        raise ValueError("Этот ник уже занят")
    token = secrets.token_urlsafe(32)
    participant = TabletopParticipant(
        id=str(uuid.uuid4()),
        room_id=room.id,
        nickname=nickname,
        token_hash=hash_player_token(token),
        seat=count,
        role_data={},
        reminders=[],
        is_alive=True,
    )
    db.add(participant)
    await db.commit()
    await db.refresh(participant)
    return participant, token


async def authenticate_player(
    db: AsyncSession, room: TabletopRoom, token: str | None
) -> TabletopParticipant | None:
    if not token:
        return None
    return await db.scalar(
        select(TabletopParticipant).where(
            TabletopParticipant.room_id == room.id,
            TabletopParticipant.token_hash == hash_player_token(token),
        )
    )


def _prepare_role_data(
    role: RoleDefinition,
    game: GameDefinition,
    assigned_role_ids: set[str],
    eligible_role_ids: set[str],
) -> dict[str, Any]:
    data: dict[str, Any] = role_payload(role)
    if role.id == "drunk":
        candidates = [
            item
            for item in game.role_catalog
            if item.team == "townsfolk" and item.id in eligible_role_ids and item.id not in assigned_role_ids
        ]
        if candidates:
            data["perceived_role"] = role_payload(secrets.choice(candidates))
    return data


async def start_room(db: AsyncSession, room: TabletopRoom) -> TabletopRoom:
    await db.execute(select(TabletopRoom.id).where(TabletopRoom.id == room.id).with_for_update())
    if room.status != "lobby":
        raise ValueError("Комната уже запущена")
    participants = list(
        (
            await db.execute(
                select(TabletopParticipant)
                .where(TabletopParticipant.room_id == room.id)
                .order_by(TabletopParticipant.seat)
            )
        )
        .scalars()
        .all()
    )
    game = game_registry.get(room.game_id)
    if not game:
        raise ValueError("Игра больше не установлена")
    if not game.min_players <= len(participants) <= game.max_players:
        raise ValueError(f"Для старта нужно от {game.min_players} до {game.max_players} игроков")
    roles = game.assign_roles(len(participants), room.config)
    assigned_ids = {role.id for role in roles}
    eligible_role_ids = set(
        game.metadata.get("script_role_ids", {}).get(room.config.get("script"), assigned_ids)
    )
    for participant, role in zip(participants, roles, strict=True):
        participant.role_id = role.id
        participant.role_data = _prepare_role_data(role, game, assigned_ids, eligible_role_ids)
        participant.is_alive = True
    good_bluffs = [
        item
        for item in game.role_catalog
        if item.team in {"townsfolk", "outsider"}
        and item.id in eligible_role_ids
        and item.id not in assigned_ids
    ]
    room.game_state = {
        "demon_bluffs": [role_payload(item) for item in secrets.SystemRandom().sample(good_bluffs, 3)]
        if len(good_bluffs) >= 3
        else []
    }
    room.status = "playing"
    room.started_at = datetime.now(UTC)
    await db.commit()
    return room


async def active_participant_ids(room_id: str, participants: list[TabletopParticipant]) -> set[str]:
    keys = [f"tabletop:presence:{room_id}:{participant.id}" for participant in participants]
    if not keys:
        return set()
    try:
        values = await redis_client.mget(keys)
    except Exception:
        return set()
    return {participant.id for participant, value in zip(participants, values, strict=True) if value}


def participant_public(
    participant: TabletopParticipant, online_ids: set[str], *, show_online: bool = True
) -> dict[str, Any]:
    return {
        "id": participant.id,
        "nickname": participant.nickname,
        "seat": participant.seat,
        "is_alive": participant.is_alive,
        "online": participant.id in online_ids if show_online else None,
    }


def message_payload(message: TabletopMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "sender_id": message.sender_participant_id,
        "sender": message.sender.nickname if message.sender else "Ведущий",
        "recipient_id": message.recipient_participant_id,
        "recipient": message.recipient.nickname if message.recipient else None,
        "audience": message.audience,
        "text": message.text,
        "created_at": message.created_at.isoformat(),
    }


async def room_messages(
    db: AsyncSession, room_id: str, participant_id: str | None = None
) -> list[dict[str, Any]]:
    query = (
        select(TabletopMessage)
        .where(TabletopMessage.room_id == room_id)
        .options(selectinload(TabletopMessage.sender), selectinload(TabletopMessage.recipient))
        .order_by(TabletopMessage.id.desc())
        .limit(200)
    )
    if participant_id:
        query = query.where(
            or_(
                TabletopMessage.audience == "broadcast",
                TabletopMessage.sender_participant_id == participant_id,
                and_(
                    TabletopMessage.audience == "player",
                    TabletopMessage.recipient_participant_id == participant_id,
                ),
            )
        )
    messages = list((await db.execute(query)).scalars().unique())
    return [message_payload(item) for item in reversed(messages)]


async def owner_state(db: AsyncSession, room: TabletopRoom) -> dict[str, Any]:
    participants = list(
        (
            await db.execute(
                select(TabletopParticipant)
                .where(TabletopParticipant.room_id == room.id)
                .order_by(TabletopParticipant.seat)
            )
        )
        .scalars()
        .all()
    )
    online_ids = await active_participant_ids(room.id, participants)
    return {
        "room": room_payload(room),
        "participants": [
            {
                **participant_public(item, online_ids),
                "role": item.role_data or None,
                "reminders": item.reminders,
                "gm_notes": item.gm_notes,
            }
            for item in participants
        ],
        "messages": await room_messages(db, room.id),
    }


async def player_state(
    db: AsyncSession, room: TabletopRoom, participant: TabletopParticipant
) -> dict[str, Any]:
    participants = list(
        (
            await db.execute(
                select(TabletopParticipant)
                .where(TabletopParticipant.room_id == room.id)
                .order_by(TabletopParticipant.seat)
            )
        )
        .scalars()
        .all()
    )
    online_ids = await active_participant_ids(room.id, participants)
    visible_role = None
    knowledge: dict[str, Any] = {}
    evil_info = room.config.get("evil_info", "standard")
    reveal_evil = evil_info == "always" or (evil_info == "standard" and len(participants) >= 7)
    if room.status != "lobby" and participant.role_data:
        visible_role = participant.role_data.get("perceived_role", participant.role_data)
        if reveal_evil and participant.role_data.get("team") == "demon":
            knowledge = {
                "evil_team": [
                    item.nickname
                    for item in participants
                    if item.id != participant.id and item.role_data.get("team") == "minion"
                ],
                "bluffs": room.game_state.get("demon_bluffs", []),
            }
        elif reveal_evil and participant.role_data.get("team") == "minion":
            knowledge = {
                "evil_team": [
                    item.nickname
                    for item in participants
                    if item.id != participant.id and item.role_data.get("team") in {"minion", "demon"}
                ]
            }
    return {
        "room": room_payload(room),
        "me": {
            **participant_public(participant, online_ids),
            "role": visible_role,
            "knowledge": knowledge,
        },
        "participants": [
            {
                **participant_public(
                    item,
                    online_ids,
                    show_online=room.config.get("show_online_status", True),
                ),
                "role": (
                    item.role_data or None
                    if room.status == "ended" and room.config.get("reveal_roles_on_end", True)
                    else None
                ),
            }
            for item in participants
        ],
        "messages": await room_messages(db, room.id, participant.id),
    }


def room_payload(room: TabletopRoom) -> dict[str, Any]:
    return {
        "id": room.id,
        "code": room.code,
        "game_id": room.game_id,
        "title": room.title,
        "status": room.status,
        "config": room.config,
        "created_at": room.created_at.isoformat(),
        "started_at": room.started_at.isoformat() if room.started_at else None,
    }
