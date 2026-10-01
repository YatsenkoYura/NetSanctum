"""Owner-scoped session context: the short memory behind REST and socket turns.

A stored conversation is the source of truth for what was said. For threads the
model reads a short window rebuilt from the transcript (see conversations.py).
For ephemeral turns without a thread, the bounded history + references live here
in Redis for 15 minutes. Either way the model never sees the whole transcript.
"""

import json
import logging
import secrets

from pydantic import ValidationError

from app.core.security import redis_client
from app.modules.miku.schemas import MikuConversationTurn, MikuReference
from app.modules.miku.service import MikuSessionContext

logger = logging.getLogger(__name__)

REST_CONTEXT_TTL_SECONDS = 900
REST_CONTEXT_LOCK_SECONDS = 300

RELEASE_CONTEXT_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def rest_context(context_id: str | None, user_id: int) -> MikuSessionContext | None:
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


async def save_rest_context(context_id: str | None, user_id: int, context: MikuSessionContext | None) -> None:
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


async def acquire_context_lock(context_id: str | None, user_id: int) -> tuple[str, str] | None:
    if not context_id:
        return None
    key = f"miku:context-lock:{user_id}:{context_id}"
    token = secrets.token_urlsafe(18)
    if not await redis_client.set(key, token, ex=REST_CONTEXT_LOCK_SECONDS, nx=True):
        from app.modules.miku.service import MikuQueryError

        raise MikuQueryError("Session context is busy; retry the request.")
    return key, token


async def release_context_lock(lock: tuple[str, str] | None) -> None:
    if not lock:
        return
    key, token = lock
    try:
        await redis_client.eval(RELEASE_CONTEXT_LOCK_SCRIPT, 1, key, token)
    except Exception:
        logger.warning("MIKU context lock release failed", exc_info=True)


__all__ = [
    "REST_CONTEXT_LOCK_SECONDS",
    "REST_CONTEXT_TTL_SECONDS",
    "acquire_context_lock",
    "release_context_lock",
    "rest_context",
    "save_rest_context",
]
