import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.storage import get_storage

logger = logging.getLogger(__name__)
_task: asyncio.Task | None = None
_status: dict[str, Any] = {
    "running": False,
    "migrated_total": 0,
    "current": 0,
    "unreadable": 0,
    "pending": 0,
    "last_run": None,
    "last_error": None,
}


def encryption_migration_status() -> dict[str, Any]:
    return dict(_status)


async def _migration_loop() -> None:
    settings = get_settings()
    storage = get_storage()
    _status["running"] = True
    try:
        while True:
            try:
                result = await asyncio.to_thread(
                    storage.migrate_legacy_encryption_batch,
                    max(1, settings.ENCRYPTION_MIGRATION_BATCH_SIZE),
                )
                _status.update(
                    migrated_total=_status["migrated_total"] + result.migrated,
                    current=result.current,
                    unreadable=result.unreadable,
                    pending=result.pending,
                    last_run=datetime.now(UTC).isoformat(),
                    last_error=None,
                )
                if result.migrated:
                    logger.info("Migrated %s legacy encrypted storage object(s)", result.migrated)
                if result.examined or result.pending:
                    delay = settings.ENCRYPTION_MIGRATION_INTERVAL_SECONDS
                else:
                    if result.unreadable:
                        logger.warning(
                            "%s encrypted storage object(s) need an unavailable legacy key",
                            result.unreadable,
                        )
                    delay = settings.ENCRYPTION_MIGRATION_IDLE_SECONDS
            except Exception as error:
                _status.update(
                    last_run=datetime.now(UTC).isoformat(),
                    last_error=f"{type(error).__name__}: {error}",
                )
                logger.exception("Background encryption migration failed")
                delay = settings.ENCRYPTION_MIGRATION_IDLE_SECONDS
            await asyncio.sleep(max(delay, 0.1))
    finally:
        _status["running"] = False


def start_encryption_migration() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_migration_loop(), name="encryption-migration")


async def stop_encryption_migration() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    with suppress(asyncio.CancelledError):
        await _task
    _task = None
