"""Extractive consolidation: old threads become episodic summaries.

No LLM involved on purpose: the worker writes what happened (title, first
question, last answer) so recall has something to find. The model-grade
summarization can replace this later without changing the tables.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import distinct, select

from app.modules.miku.conversations import list_messages
from app.modules.miku.models import MikuConversation, MikuEpisodeMemory

logger = logging.getLogger(__name__)

CONSOLIDATION_AGE_HOURS = 24
MAX_CONVERSATIONS_PER_PASS = 10


def _clip(value: str, limit: int) -> str:
    return " ".join((value or "").split())[:limit]


async def consolidate_old_conversations(db, *, limit: int = MAX_CONVERSATIONS_PER_PASS) -> int:
    """Write one episodic summary per stale thread that has none yet."""
    cutoff = datetime.now(UTC) - timedelta(hours=CONSOLIDATION_AGE_HOURS)
    user_ids = list(await db.scalars(select(distinct(MikuConversation.user_id))))
    written = 0
    for user_id in user_ids:
        threads = (
            await db.scalars(
                select(MikuConversation)
                .where(
                    MikuConversation.user_id == user_id,
                    MikuConversation.updated_at < cutoff,
                )
                .order_by(MikuConversation.updated_at.desc())
                .limit(limit)
            )
        ).all()
        for thread in threads:
            if written >= limit:
                break
            messages = await list_messages(db, thread.id, limit=200)
            if len(messages) < 2:
                continue
            first_user = next((m.content for m in messages if m.role == "user"), "")
            last_assistant = next((m.content for m in reversed(messages) if m.role == "assistant"), "")
            summary = _clip(f"{thread.title or 'Диалог'}: {first_user} → {last_assistant}", 500)
            if not summary:
                continue
            exists = await db.scalar(
                select(MikuEpisodeMemory.id).where(
                    MikuEpisodeMemory.user_id == user_id,
                    MikuEpisodeMemory.summary == summary,
                )
            )
            if exists:
                continue
            db.add(
                MikuEpisodeMemory(
                    user_id=user_id,
                    summary=summary,
                    subject=_clip(thread.title, 160) or None,
                    tags_json=[],
                    source="derived",
                    occurred_at=thread.updated_at,
                )
            )
            written += 1
    if written:
        await db.flush()
    return written


__all__ = ["consolidate_old_conversations"]
