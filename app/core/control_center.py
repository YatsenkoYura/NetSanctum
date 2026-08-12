"""Runtime observability and safe administrative actions for the Control Center."""

import asyncio
import json
import platform
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.module_config import load_enabled_module_ids
from app.core.modules import ModuleStatus, module_registry

PROCESS_STARTED_AT = time.monotonic()
TASK_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _redis_client():
    return aioredis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)


def _human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


async def _database_health() -> dict[str, Any]:
    started = time.perf_counter()
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "online", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        return {"status": "offline", "error": str(exc)[:180]}


async def _redis_health() -> dict[str, Any]:
    started = time.perf_counter()
    client = _redis_client()
    try:
        await client.ping()
        info = await client.info("memory")
        return {
            "status": "online",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "memory": _human_bytes(int(info.get("used_memory", 0))),
            "keys": await client.dbsize(),
        }
    except Exception as exc:
        return {"status": "offline", "error": str(exc)[:180]}
    finally:
        await client.aclose()


def _inspect_celery() -> dict[str, Any]:
    try:
        from app.core.scheduler import celery_app

        pings = celery_app.control.ping(timeout=0.6)
        inspector = celery_app.control.inspect(timeout=0.6)
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        return {
            "status": "online" if pings else "offline",
            "workers": sorted(worker for reply in pings for worker in reply),
            "active": active,
            "reserved": reserved,
            "active_count": sum(len(tasks) for tasks in active.values()),
            "reserved_count": sum(len(tasks) for tasks in reserved.values()),
        }
    except Exception as exc:
        return {"status": "offline", "error": str(exc)[:180], "workers": []}


async def runtime_overview() -> dict[str, Any]:
    database, redis_status, celery = await asyncio.gather(
        _database_health(),
        _redis_health(),
        asyncio.to_thread(_inspect_celery),
    )
    installed = [record for record in module_registry.records if module_registry.is_installed(record.id)]
    return {
        "database": database,
        "redis": redis_status,
        "celery": celery,
        "modules": {
            "active": len(module_registry.active_records()),
            "installed": len(installed),
            "failed": sum(record.status == ModuleStatus.FAILED for record in module_registry.records),
        },
        "runtime": {
            "app_version": get_settings().APP_VERSION,
            "python": platform.python_version(),
            "platform": sys.platform,
            "uptime_seconds": int(time.monotonic() - PROCESS_STARTED_AT),
        },
    }


def module_control_state() -> dict[str, Any]:
    desired_ids, source = load_enabled_module_ids()
    rows = []
    restart_required = False
    for record in module_registry.records:
        spec = record.spec
        if not spec:
            continue
        installed = module_registry.is_installed(record.id)
        desired = spec.required or (installed and (desired_ids is None or record.id in desired_ids))
        actual = record.status == ModuleStatus.ACTIVE
        if installed and desired != actual:
            restart_required = True
        rows.append(
            {
                "id": record.id,
                "title_en": spec.title_en,
                "title_ru": spec.title_ru,
                "version": spec.version,
                "status": record.status.value,
                "installed": installed,
                "required": spec.required,
                "desired": desired,
                "error": record.error,
                "component_errors": record.component_errors or {},
            }
        )
    return {
        "modules": rows,
        "source": source,
        "restart_required": restart_required,
        "environment_locked": bool(get_settings().ENABLED_MODULES.strip()),
    }


async def tracked_tasks() -> list[dict[str, Any]]:
    client = _redis_client()
    tasks = []
    seen_keys = set()
    try:
        for record in module_registry.declared_records():
            if not record.spec:
                continue
            for pattern in record.spec.progress_key_patterns:
                async for key in client.scan_iter(match=pattern, count=100):
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    raw = await client.get(key)
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {"status": "invalid tracker payload"}
                    payload.update(
                        {
                            "key": key,
                            "module": record.id,
                            "task_id": payload.get("task_id") or key.rsplit(":", 1)[-1],
                            "ttl": await client.ttl(key),
                        }
                    )
                    tasks.append(payload)
        return sorted(tasks, key=lambda item: (item.get("module", ""), item.get("title", "")))
    finally:
        await client.aclose()


def tasks_blocking_module_change(selected: set[str], tasks: list[dict[str, Any]]) -> set[str]:
    disabling = {
        record.id
        for record in module_registry.active_records()
        if record.spec and not record.spec.required and record.id not in selected
    }
    return {
        module_id
        for task in tasks
        if isinstance((module_id := task.get("module")), str) and module_id in disabling
    }


async def cancel_tracked_task(task_id: str) -> int:
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise ValueError("Invalid task id")
    from app.core.scheduler import celery_app

    await asyncio.to_thread(celery_app.control.revoke, task_id, terminate=True)
    client = _redis_client()
    deleted = 0
    try:
        for record in module_registry.declared_records():
            if not record.spec:
                continue
            for pattern in record.spec.progress_key_patterns:
                async for key in client.scan_iter(match=pattern, count=100):
                    raw = await client.get(key)
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        payload = {}
                    if payload.get("task_id") == task_id or key.endswith(f":{task_id}"):
                        deleted += await client.delete(key)
        return deleted
    finally:
        await client.aclose()


async def read_logs(
    *, limit: int = 200, level: str | None = None, role: str | None = None, query: str | None = None
) -> list[dict[str, Any]]:
    settings = get_settings()
    client = _redis_client()
    try:
        raw_entries = await client.lrange(settings.OBSERVABILITY_LOG_KEY, 0, min(limit * 5, 999))
    finally:
        await client.aclose()

    entries = []
    normalized_query = query.lower().strip() if query else None
    for raw in raw_entries:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if level and entry.get("level") != level.upper():
            continue
        if role and entry.get("role") != role:
            continue
        if normalized_query and normalized_query not in (
            f"{entry.get('logger', '')} {entry.get('message', '')}".lower()
        ):
            continue
        entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


async def clear_logs() -> None:
    client = _redis_client()
    try:
        await client.delete(get_settings().OBSERVABILITY_LOG_KEY)
    finally:
        await client.aclose()


def format_timestamp(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value).astimezone(UTC).strftime("%H:%M:%S")
    except ValueError:
        return value[:19]
