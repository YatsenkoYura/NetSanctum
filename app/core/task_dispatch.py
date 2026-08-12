"""Tracked Celery dispatch helpers with tracker-before-broker ordering."""

import json
import uuid
from typing import Any


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
