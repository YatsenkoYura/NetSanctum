"""Background notices for the owner: finished tasks, ready downloads, briefings.

The worker cannot reach open sockets, so notices wait in Redis per owner. The
side chat polls `GET /api/miku/notifications` and prints them as status lines.
Polling is deliberate: no socket registry to keep in sync across processes.
"""

import json
import time

from app.core.security import redis_client

NOTIFICATION_TTL_SECONDS = 7 * 24 * 3600
MAX_NOTIFICATIONS = 20


async def push_notification(user_id: int, text: str, *, kind: str = "status") -> None:
    """Queue one notice. Worker-side: never raises into the task loop."""
    text = " ".join((text or "").split())[:300]
    if not text:
        return
    try:
        key = f"miku:notify:{user_id}"
        payload = json.dumps({"text": text, "kind": kind, "at": int(time.time())})
        pipe = redis_client.pipeline(transaction=False)
        pipe.lpush(key, payload)
        pipe.ltrim(key, 0, MAX_NOTIFICATIONS - 1)
        pipe.expire(key, NOTIFICATION_TTL_SECONDS)
        await pipe.execute()
    except Exception:
        return


async def pop_notifications(user_id: int) -> list[dict]:
    """Take all pending notices, oldest first. Poll semantics: read clears."""
    key = f"miku:notify:{user_id}"
    try:
        raw = await redis_client.lrange(key, 0, MAX_NOTIFICATIONS - 1)
        if raw:
            await redis_client.delete(key)
    except Exception:
        return []
    items = []
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


__all__ = ["pop_notifications", "push_notification"]
