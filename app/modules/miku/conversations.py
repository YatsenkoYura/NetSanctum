"""Dialogue threads: the transcript that outlives a session, and the window into it.

Two different things live here and it matters which is which. The transcript is
durable: every message is stored, so a conversation can be closed and reopened
days later. The window is what the model actually reads, and it is deliberately
short - a long conversation replayed into every prompt grows without bound and
dilutes what the model attends to. What falls outside the window the agent
carries forward itself, as notes.
"""

from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.miku.models import (
    MikuConversation,
    MikuConversationMessage,
    MikuConversationNote,
)

CONVERSATION_TITLE_LIMIT = 120
MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_ASSISTANT = "assistant"
# How many recent turns the model is shown. Older material is expected to live in
# notes; the transcript keeps it for the person reading it.
MODEL_WINDOW_TURNS = 6
MAX_MESSAGES_RETURNED = 200


def _now() -> datetime:
    return datetime.now(UTC)


def title_from_message(message: str) -> str:
    """Name a conversation after its first line.

    Asking the model for a title would cost a full round trip on a small local model,
    which is the slowest thing in the request; the opening line is what a person
    recognises the thread by anyway.
    """
    collapsed = " ".join((message or "").split())
    if not collapsed:
        return "Новый диалог"
    if len(collapsed) <= CONVERSATION_TITLE_LIMIT:
        return collapsed
    return collapsed[:CONVERSATION_TITLE_LIMIT].rstrip() + "…"


async def create_conversation(
    db: AsyncSession,
    user_id: int,
    *,
    title: str = "",
) -> MikuConversation:
    # The title stays empty until the first thing is said in the thread; the list
    # shows a placeholder for one that is still empty rather than freezing a name
    # that the first message would have given a better one.
    conversation = MikuConversation(user_id=user_id, title=title.strip()[:CONVERSATION_TITLE_LIMIT])
    db.add(conversation)
    await db.flush()
    return conversation


async def get_conversation(db: AsyncSession, user_id: int, conversation_id: int) -> MikuConversation | None:
    return await db.scalar(
        select(MikuConversation).where(
            MikuConversation.id == conversation_id,
            MikuConversation.user_id == user_id,
        )
    )


async def list_conversations(
    db: AsyncSession,
    user_id: int,
    *,
    limit: int = 50,
) -> list[tuple[MikuConversation, int]]:
    """Most recently touched first, with the message count the list shows."""
    counts = (
        select(
            MikuConversationMessage.conversation_id,
            func.count(MikuConversationMessage.id),
        )
        .group_by(MikuConversationMessage.conversation_id)
        .subquery()
    )
    rows = await db.execute(
        select(MikuConversation, func.coalesce(counts.c[1], 0))
        .outerjoin(counts, counts.c[0] == MikuConversation.id)
        .where(
            MikuConversation.user_id == user_id,
            MikuConversation.archived.is_(False),
        )
        .order_by(MikuConversation.updated_at.desc())
        .limit(limit)
    )
    return [(conversation, int(count)) for conversation, count in rows.all()]


async def rename_conversation(
    db: AsyncSession, conversation: MikuConversation, title: str
) -> MikuConversation:
    conversation.title = title.strip()[:CONVERSATION_TITLE_LIMIT]
    conversation.updated_at = _now()
    await db.flush()
    return conversation


async def delete_conversation(db: AsyncSession, conversation: MikuConversation) -> None:
    """Remove the thread and everything hanging off it."""
    await db.execute(
        delete(MikuConversationMessage).where(MikuConversationMessage.conversation_id == conversation.id)
    )
    await db.execute(
        delete(MikuConversationNote).where(MikuConversationNote.conversation_id == conversation.id)
    )
    await db.delete(conversation)
    await db.flush()


async def list_messages(
    db: AsyncSession,
    conversation_id: int,
    *,
    limit: int = MAX_MESSAGES_RETURNED,
) -> list[MikuConversationMessage]:
    rows = await db.scalars(
        select(MikuConversationMessage)
        .where(MikuConversationMessage.conversation_id == conversation_id)
        .order_by(MikuConversationMessage.id.desc())
        .limit(limit)
    )
    # The query reads newest first only to bound it; the transcript reads forwards.
    return list(reversed(list(rows)))


async def append_message(
    db: AsyncSession,
    conversation: MikuConversation,
    role: str,
    content: str,
    *,
    command: str | None = None,
    cascade_id: int | None = None,
) -> MikuConversationMessage:
    message = MikuConversationMessage(
        conversation_id=conversation.id,
        role=role,
        content=content,
        command=command,
        cascade_id=cascade_id,
    )
    db.add(message)
    if not conversation.title and role == MESSAGE_ROLE_USER:
        conversation.title = title_from_message(content)
    conversation.updated_at = _now()
    await db.flush()
    return message


async def model_window(
    db: AsyncSession,
    conversation_id: int,
    *,
    turns: int = MODEL_WINDOW_TURNS,
) -> list[tuple[str, str]]:
    """The recent turns as (user, assistant) pairs, oldest first.

    A turn is a user message and the reply that answered it; an unanswered message at
    the end is left out rather than paired with nothing, since the model is about to
    be asked that question anyway.
    """
    rows = await list_messages(db, conversation_id, limit=turns * 2 * 4)
    pairs: list[tuple[str, str]] = []
    pending: str | None = None
    for message in rows:
        if message.role == MESSAGE_ROLE_USER:
            if pending is not None:
                continue
            pending = message.content
        elif message.role == MESSAGE_ROLE_ASSISTANT and pending is not None:
            pairs.append((pending, message.content))
            pending = None
    return pairs[-turns:]


async def touch(db: AsyncSession, conversation: MikuConversation) -> None:
    await db.execute(
        update(MikuConversation).where(MikuConversation.id == conversation.id).values(updated_at=_now())
    )
    await db.flush()


__all__ = [
    "CONVERSATION_TITLE_LIMIT",
    "MAX_MESSAGES_RETURNED",
    "MESSAGE_ROLE_ASSISTANT",
    "MESSAGE_ROLE_USER",
    "MODEL_WINDOW_TURNS",
    "append_message",
    "create_conversation",
    "delete_conversation",
    "get_conversation",
    "list_conversations",
    "list_messages",
    "model_window",
    "rename_conversation",
    "title_from_message",
    "touch",
]
