"""Read every Vault file end to end and report what does not open.

Counting a file proves nothing. The migration counts, the player reads the range it
needs, and a chunk that went bad three hours into a video is discovered when
somebody watches that hour — if they get that far. This walks every chunk of every
file a Vault row points at, re-derives each tag, and says so plainly.

The check is per key, so it runs per collection: a sealed collection's files are
sealed under its own file key, and that key only exists while somebody has the
vault open. Plain collections are read under the application key.

    docker compose exec web python -m scripts.vault_verify_media --report
    docker compose exec web python -m scripts.vault_verify_media --report --collection 4
    docker compose exec web python -m scripts.vault_verify_media --report --unlock-token "$TOKEN"

`plaintext` files are listed, not failed: they carry no envelope, so there is no tag
to check. They are reported because a Vault row pointing at an unencrypted file is
either legacy media or something that was never protected, and both are worth seeing.

Nothing is written in any mode. `--unlock-token` is only needed for sealed
collections; without it they are reported as skipped rather than as failures,
because "cannot check" is not the same answer as "broken" and conflating the two
would make the report a lie in the direction that hides failures.

Exit code is 1 when something failed to open, so this can gate a backup.
"""

import argparse
import asyncio
import json
import sys

from app.core.database import AsyncSessionLocal
from app.core.storage import get_storage
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.sealing import data_key_for, derive_file_key


async def collect(session, collection_id: int | None) -> list[tuple[str, int | None, int]]:
    """Every file a card points at: its stored path, its collection, its row id."""
    query = VaultItem.__table__.select().with_only_columns(
        VaultItem.__table__.c.image_path,
        VaultItem.__table__.c.media_path,
        VaultItem.__table__.c.collection_id,
        VaultItem.__table__.c.id,
    )
    if collection_id is not None:
        query = query.where(VaultItem.__table__.c.collection_id == collection_id)
    rows = (await session.execute(query)).all()
    found: list[tuple[str, int | None, int]] = []
    for image_path, media_path, row_collection, row_id in rows:
        for path in (image_path, media_path):
            if path:
                found.append((str(path), row_collection, row_id))
    return found


async def run(collection_id: int | None, unlock_token: str) -> dict[str, object]:
    storage = get_storage()
    failures: list[dict[str, object]] = []
    skipped_paths: list[dict[str, object]] = []
    plaintext_paths: list[dict[str, object]] = []
    checked = ok = skipped = failed = plaintext = 0
    async with AsyncSessionLocal() as session:
        targets = await collect(session, collection_id)
        keys: dict[int | None, bytes | None] = {}
        for path, row_collection, row_id in targets:
            if row_collection not in keys:
                collection = await session.get(VaultCollection, row_collection)
                if collection is None or not collection.is_encrypted:
                    keys[row_collection] = None
                else:
                    private_key = await data_key_for(collection, unlock_token)
                    keys[row_collection] = (
                        None if private_key is None else derive_file_key(private_key, collection.id)
                    )
            key = keys[row_collection]
            if key is None and row_collection is not None:
                locked = await session.get(VaultCollection, row_collection)
                if locked is not None and locked.is_encrypted:
                    skipped += 1
                    skipped_paths.append({"path": path, "item": row_id, "why": "vault locked"})
                    continue
            try:
                report = await asyncio.to_thread(storage.verify_encrypted_object, path, key=key)
                if report.get("envelope") == "plaintext":
                    # Not encrypted at all. A fact about the file, not damage — but the
                    # operator should see it, because a Vault row pointing at plaintext
                    # is either legacy media or something that was never protected.
                    plaintext += 1
                    plaintext_paths.append({"path": path, "item": row_id})
                    checked += 1
                    continue
            except FileNotFoundError:
                failed += 1
                failures.append({"path": path, "item": row_id, "error": "missing"})
                continue
            except Exception as error:
                failed += 1
                failures.append(
                    {"path": path, "item": row_id, "error": f"{type(error).__name__}: {error}"[:200]}
                )
                continue
            checked += 1
            ok += 1
    return {
        "checked": checked,
        "ok": ok,
        "plaintext": plaintext,
        "plaintext_paths": plaintext_paths,
        "skipped": skipped,
        "failed": failed,
        "failures": failures,
        "skipped_paths": skipped_paths,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--collection", type=int, default=None, help="one collection only")
    parser.add_argument("--unlock-token", default="", help="a tab's unlock token, for sealed collections")
    parser.add_argument(
        "--report", action="store_true", help="accepted for symmetry; nothing is ever written"
    )
    args = parser.parse_args(argv)

    summary = asyncio.run(run(args.collection, args.unlock_token))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["skipped"]:
        print(
            f"\n{summary['skipped']} file(s) were not checked because their vault is locked. "
            "Pass --unlock-token to include them; an unchecked file is not a healthy one.",
            file=sys.stderr,
        )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
