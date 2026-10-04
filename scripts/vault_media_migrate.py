"""Move Vault's stored media from the v1 file envelope to v2.

The v1 chunked envelope kept its plaintext length in a header nobody
authenticated. The reader still speaks v1, so this is housekeeping — but the
migration has to be safe to interrupt, because it runs over files measured in
gigabytes, and safe to run twice, because an operator will.

    uv run python -m scripts.vault_media_migrate                  # report only
    uv run python -m scripts.vault_media_migrate --apply          # one pass
    uv run python -m scripts.vault_media_migrate --apply --limit 200
    uv run python -m scripts.vault_media_migrate --retry-failed --apply

A report never touches the filesystem, `--apply` moves at most `--limit` files,
and each file is verified by reading it back before the row is pointed at it.
Run it again until it reports nothing pending: the pass is resumable by
construction, because whether a file needs work is in its header and whether the
work is done is in the row.
"""

import argparse
import asyncio

from app.core.database import AsyncSessionLocal
from app.modules.vault.media_upgrade import (
    DEFAULT_BATCH_LIMIT,
    media_files_needing_upgrade,
    retry_failed,
    upgrade_media_batch,
    upgrade_summary,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="rewrite files instead of only reporting")
    parser.add_argument("--limit", type=int, default=DEFAULT_BATCH_LIMIT, help="files per pass")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="forget recorded failures so the next pass tries those files again",
    )
    args = parser.parse_args()

    async with AsyncSessionLocal() as session:
        if args.retry_failed:
            cleared = await retry_failed(session)
            print(f"forgot {cleared} recorded failures")

        if not args.apply:
            pending = await media_files_needing_upgrade(session, limit=args.limit)
            print(f"{len(pending)} file(s) would be examined:")
            for item, column, path in pending:
                print(f"  item {item.id} {column}: {path}")
            summary = await upgrade_summary(session)
            print(f"\nledger: {summary}")
            print("nothing was written; re-run with --apply")
            return 0

        result = await upgrade_media_batch(session, limit=args.limit)
        print(
            f"upgraded {result.upgraded}, already current {result.already_current}, "
            f"failed {result.failed}, missing {result.missing}, "
            f"skipped as failed {result.skipped_failed}"
        )
        summary = await upgrade_summary(session)
        print(f"ledger: {summary}")
        if result.failed or result.missing:
            print("some files did not move; see the ledger, then --retry-failed")
    return 1 if (result.failed or result.missing) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
