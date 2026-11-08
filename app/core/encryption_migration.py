import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.storage import get_storage

logger = logging.getLogger(__name__)

# Graceful-exit timer: after the last batch with pending/unreadable work,
# the loop keeps sleeping for ENCRYPTION_MIGRATION_IDLE_SECONDS so that a
# late-arriving file is still picked up.  If _quiet_start is set and more
# than 24 hours have passed since the first quiet batch, a one-off warning
# is emitted to alert the operator that the migration can be disabled.
_QUIET_START: float | None = None
_QUIET_SECONDS = 86400  # 24 hours

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
    global _QUIET_START
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

                quiet = result.pending == 0 and result.unreadable == 0 and not result.examined
                if quiet:
                    if _QUIET_START is None:
                        _QUIET_START = datetime.now(UTC).timestamp()
                        logger.info("Encryption migration idle — no pending or unreadable objects")
                    idle = settings.ENCRYPTION_MIGRATION_IDLE_SECONDS
                    # After 24 h of idleness, emit a one-off warning that the
                    # migration loop can be stopped or ENCRYPTION_ALLOW_LEGACY_KEYS set to False.
                    if (
                        _QUIET_START is not None
                        and (datetime.now(UTC).timestamp() - _QUIET_START) > _QUIET_SECONDS
                    ):
                        logger.warning(
                            "Encryption migration has been idle for >24 h; "
                            "consider setting ENCRYPTION_ALLOW_LEGACY_KEYS=False "
                            "or stopping the background task"
                        )
                        _QUIET_START = None  # next cycle re-emits
                else:
                    _QUIET_START = None  # reset on new work
                    if result.unreadable:
                        logger.warning(
                            "%s encrypted storage object(s) need an unavailable legacy key",
                            result.unreadable,
                        )
                    idle = (
                        settings.ENCRYPTION_MIGRATION_INTERVAL_SECONDS
                        if (result.examined or result.pending)
                        else settings.ENCRYPTION_MIGRATION_IDLE_SECONDS
                    )

                await asyncio.sleep(idle)
            except asyncio.CancelledError:
                logger.info("Encryption migration task cancelled")
                raise
            except Exception as error:
                _quiet_start = None  # reset on error
                _status.update(
                    last_run=datetime.now(UTC).isoformat(),
                    last_error=f"{type(error).__name__}: {error}",
                )
                logger.exception("Background encryption migration failed")
                await asyncio.sleep(settings.ENCRYPTION_MIGRATION_IDLE_SECONDS)
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
