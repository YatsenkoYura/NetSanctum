"""Tracked Celery dispatch helpers with tracker-before-broker ordering."""

import json
import uuid
from typing import Any

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})


def is_terminal_task_payload(payload: dict[str, Any]) -> bool:
    if str(payload.get("state") or "").lower() in TERMINAL_TASK_STATES:
        return True
    status = str(payload.get("status") or "").strip().lower()
    title = str(payload.get("title") or "").strip().lower()
    return (
        status in {"completed", "already available", "conversion completed", "error"}
        or status.startswith(("failed:", "error:"))
        or title == "error"
        or title.endswith("(finished)")
    )


def _task_payload(task_id: str, payload: dict[str, Any]) -> str:
    return json.dumps({**payload, "task_id": task_id})


async def dispatch_tracked_async(
    task,
    redis_client,
    key_prefix: str,
    payload: dict[str, Any],
    *,
    args: tuple = (),
    kwargs: dict[str, Any] | None = None,
    ttl: int = 86400,
):
    task_id = str(uuid.uuid4())
    key = f"{key_prefix}:{task_id}"
    await redis_client.setex(key, ttl, _task_payload(task_id, payload))
    try:
        return task.apply_async(args=args, kwargs=kwargs or {}, task_id=task_id)
    except Exception:
        await redis_client.delete(key)
        raise


def dispatch_tracked_sync(
    task,
    redis_client,
    key_prefix: str,
    payload: dict[str, Any],
    *,
    args: tuple = (),
    kwargs: dict[str, Any] | None = None,
    ttl: int = 86400,
):
    task_id = str(uuid.uuid4())
    key = f"{key_prefix}:{task_id}"
    redis_client.setex(key, ttl, _task_payload(task_id, payload))
    try:
        return task.apply_async(args=args, kwargs=kwargs or {}, task_id=task_id)
    except Exception:
        redis_client.delete(key)
        raise
