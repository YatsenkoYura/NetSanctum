"""Moving Vault's stored media from the v1 file envelope to v2.

The v1 chunked envelope left its plaintext length in an unauthenticated header:
rewrite it and every chunk still verified while the size helpers reported
something else. v2 binds the length, the chunk size and the chunk count into
every chunk. The reader speaks both, so this is housekeeping rather than a
repair — but a header nobody authenticates is worth not having.

The unit of work is one file. The envelope binds its own path, so a v2 object
cannot overwrite a v1 path: each file is written to a new name, verified by
reading it back, and only then does the row point at it. The old file is
deleted last, and deleting it last is the whole trick — every crash between the
steps leaves a file that still opens, never a row pointing at nothing.

Idempotent because "does this need work" is read from the file's header and
"is it done" is read from the row that points at it, so a second pass over the
same storage finds nothing to do. Resumable for the same reason: an interrupted
run simply leaves the remaining rows on v1. The ledger table remembers only
what neither of those can — that a file was tried and failed, and why.
"""

import asyncio
import datetime
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import get_storage
from app.modules.vault.models import VaultItem, VaultMediaUpgrade

logger = logging.getLogger(__name__)

# Every column that points at a stored file. Videos, posters and images all
# move; nothing else in the row is a file.
MEDIA_FILE_COLUMNS = ("media_path", "media_thumbnail_path", "image_path")

STATE_PENDING = "pending"
STATE_DONE = "done"
STATE_FAILED = "failed"

# How many files one pass touches. A migration that runs unbounded inside a
# request or a worker slot is a migration that gets killed halfway; the pass is
# meant to be called again until it reports nothing left.
DEFAULT_BATCH_LIMIT = 25


@dataclass(frozen=True, slots=True)
class MediaUpgradeResult:
    upgraded: int = 0
    already_current: int = 0
    failed: int = 0
    missing: int = 0
    skipped_failed: int = 0

    @property
    def pending(self) -> int:
        return self.upgraded + self.failed + self.missing


def upgraded_path(path: str) -> str:
    """The v2 name for a v1 path. One spelling, shared with the storage layer."""
    return get_storage().upgraded_envelope_path(path)


async def _ledger(session, path: str) -> VaultMediaUpgrade | None:
    return await session.get(VaultMediaUpgrade, path)


async def _record(
    session,
    path: str,
    *,
    state: str,
    item_id: int | None = None,
    column_name: str | None = None,
    error: str | None = None,
    version: int | None = None,
) -> None:
    row = await session.get(VaultMediaUpgrade, path)
    if row is None:
        row = VaultMediaUpgrade(path=path)
        session.add(row)
    row.state = state
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error
    row.result_version = version
    if item_id is not None:
        row.item_id = item_id
    if column_name is not None:
        row.column_name = column_name
    row.updated_at = datetime.datetime.utcnow()
    await session.commit()


async def media_files_needing_upgrade(
    session: AsyncSession, *, limit: int = DEFAULT_BATCH_LIMIT
) -> list[tuple[VaultItem, str, str]]:
    """Rows holding a file that is a v1 envelope, oldest first.

    The database is the list, not a scan of the storage volume: the volume holds
    files this module no longer references, and walking it would mean rewriting
    objects nobody can reach through a row. The header check happens per file in
    `upgrade_media_batch`, so this stays one cheap query.
    """
    result = await session.execute(
        select(VaultItem)
        .where(
            (VaultItem.media_path.is_not(None))
            | (VaultItem.media_thumbnail_path.is_not(None))
            | (VaultItem.image_path.is_not(None))
        )
        .order_by(VaultItem.id.asc())
    )
    pending: list[tuple[VaultItem, str, str]] = []
    for item in result.scalars():
        for column in MEDIA_FILE_COLUMNS:
            path = getattr(item, column, None)
            if path:
                pending.append((item, column, path))
            if len(pending) >= limit:
                return pending
    return pending


async def upgrade_media_batch(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_BATCH_LIMIT,
    key_for: dict[int, bytes] | None = None,
) -> MediaUpgradeResult:
    """Move up to `limit` v1 media files to v2.

    `key_for` maps a collection id to that collection's file key, for the files
    a vault key protects. Without it only the application-key files can be
    moved — which is every file written before the file key existed, and every
    file a keyless worker wrote, so a worker-side pass covers most of a vault.
    Files that need a key nobody supplied are recorded as failures with that
    reason, not silently skipped: an operator reading the report should be able
    to tell "unreadable" from "needs an unlocked vault".

    Each file: write the new object, verify it reads back, point the row at it,
    commit, then delete the old file. A failure at any step leaves the row on
    the old file, which still opens.
    """
    storage = get_storage()
    keys = key_for or {}
    upgraded = already = failed = missing = skipped = 0

    for item, column, path in await media_files_needing_upgrade(session, limit=limit):
        ledger = await _ledger(session, path)
        if ledger is not None and ledger.state == STATE_FAILED:
            # A file that already failed is not retried on every pass: a vault
            # with one corrupt video would otherwise spend the whole budget
            # re-reading it. `--retry-failed` clears the record.
            skipped += 1
            continue
        try:
            version = await asyncio.to_thread(storage.seekable_envelope_version, path)
        except (FileNotFoundError, ValueError) as error:
            missing += 1
            await _record(
                session, path, state=STATE_FAILED, item_id=item.id, column_name=column, error=str(error)
            )
            continue
        if version != 1:
            # Plain objects (a plain collection's video) and v2 files are not
            # this migration's business.
            already += 1
            continue

        await _record(session, path, state=STATE_PENDING, item_id=item.id, column_name=column)
        try:
            new_path = await asyncio.to_thread(
                storage.upgrade_seekable_envelope, path, key=keys.get(item.collection_id)
            )
        except Exception as error:
            failed += 1
            logger.warning("could not upgrade the media envelope for %s: %s", item.id, error)
            await _record(
                session,
                path,
                state=STATE_FAILED,
                item_id=item.id,
                column_name=column,
                error=str(error),
            )
            continue

        # The row moves first and the old file goes second. Anything that
        # interrupts this leaves at worst an unreferenced file, never a row
        # pointing at a path nobody wrote.
        setattr(item, column, new_path)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            failed += 1
            await _record(
                session,
                path,
                state=STATE_FAILED,
                item_id=item.id,
                column_name=column,
                error="the row could not be updated",
            )
            continue
        upgraded += 1
        await _record(session, path, state=STATE_DONE, item_id=item.id, column_name=column, version=2)
        try:
            await asyncio.to_thread(storage.delete_file, path)
        except Exception:
            # The row already points at the verified new file, so this is litter
            # rather than loss. The next pass will not see it: nothing points at
            # a v1 file any more.
            logger.debug("the superseded media object %s could not be removed", path, exc_info=True)

    return MediaUpgradeResult(
        upgraded=upgraded,
        already_current=already,
        failed=failed,
        missing=missing,
        skipped_failed=skipped,
    )


async def retry_failed(session: AsyncSession) -> int:
    """Forget the recorded failures so the next pass tries those files again."""
    rows = list((await session.execute(select(VaultMediaUpgrade))).scalars())
    cleared = 0
    for row in rows:
        if row.state == STATE_FAILED:
            row.state = STATE_PENDING
            row.last_error = None
            cleared += 1
    if cleared:
        await session.commit()
    return cleared


async def upgrade_summary(session: AsyncSession) -> dict[str, int]:
    """Ledger counts by state, for a report."""
    summary = {STATE_PENDING: 0, STATE_DONE: 0, STATE_FAILED: 0}
    for (state,) in (await session.execute(select(VaultMediaUpgrade.state))).all():
        summary[state] = summary.get(state, 0) + 1
    return summary
