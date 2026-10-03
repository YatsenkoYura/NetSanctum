"""Background notices for the owner: reminders, finished tasks, ready downloads.

A worker cannot reach open sockets, so notices wait in Redis per owner and the
web side polls for them. Polling is deliberate: no socket registry to keep in
sync across processes.

This lives in core because it is infrastructure, not a feature: the planner
sweeps reminders from a Celery worker and the assistant pushes its own notices,
so neither may depend on the other's module to deliver a message.
"""

import json
import time

import redis

from app.core.config import get_settings
from app.core.security import redis_client

NOTIFICATION_TTL_SECONDS = 7 * 24 * 3600
MAX_NOTIFICATIONS = 20
# Unchanged key: notices already queued must survive this module moving.
_KEY_TEMPLATE = "miku:notify:{user_id}"

_sync_client: redis.Redis | None = None


def sync_redis_client() -> redis.Redis:
    """A synchronous client for Celery tasks.

    The shared async client is bound to the event loop of whoever created it, so
    a worker cannot reuse it. The synchronous engine has the same constraint and
    is why worker code never calls `asyncio.run` around database work.
    """
    global _sync_client
    if _sync_client is None:
        _sync_client = redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
    return _sync_client


def _payload(text: str, kind: str) -> str | None:
    cleaned = " ".join((text or "").split())[:300]
    if not cleaned:
        return None
    return json.dumps({"text": cleaned, "kind": kind, "at": int(time.time())})


async def push_notification(user_id: int, text: str, *, kind: str = "status") -> None:
    """Queue one notice. Worker-side: never raises into the task loop."""
    payload = _payload(text, kind)
    if payload is None:
        return
    try:
        key = _KEY_TEMPLATE.format(user_id=user_id)
        pipe = redis_client.pipeline(transaction=False)
        pipe.lpush(key, payload)
        pipe.ltrim(key, 0, MAX_NOTIFICATIONS - 1)
        pipe.expire(key, NOTIFICATION_TTL_SECONDS)
        await pipe.execute()
    except Exception:
        return


def push_notification_sync(user_id: int, text: str, *, kind: str = "status") -> None:
    """Queue one notice from a synchronous worker context. Never raises."""
    payload = _payload(text, kind)
    if payload is None:
        return
    try:
        key = _KEY_TEMPLATE.format(user_id=user_id)
        client = sync_redis_client()
        pipe = client.pipeline(transaction=False)
        pipe.lpush(key, payload)
        pipe.ltrim(key, 0, MAX_NOTIFICATIONS - 1)
        pipe.expire(key, NOTIFICATION_TTL_SECONDS)
        pipe.execute()
    except Exception:
        return


def _decode(raw: list[str] | None) -> list[dict]:
    items: list[dict] = []
    # Redis lists are newest-first; the reader wants oldest first.
    for entry in reversed(raw or []):
        try:
            payload = json.loads(entry)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("text"):
            items.append(
                {"text": str(payload["text"])[:300], "kind": str(payload.get("kind") or "status")[:16]}
            )
    return items


async def pop_notifications(user_id: int) -> list[dict]:
    """Take all pending notices, oldest first. Poll semantics: read clears."""
    key = _KEY_TEMPLATE.format(user_id=user_id)
    try:
        raw = await redis_client.lrange(key, 0, MAX_NOTIFICATIONS - 1)
        if raw:
            await redis_client.delete(key)
    except Exception:
        return []
    return _decode(raw)


__all__ = [
    "MAX_NOTIFICATIONS",
    "NOTIFICATION_TTL_SECONDS",
    "pop_notifications",
    "push_notification",
    "push_notification_sync",
    "sync_redis_client",
]
