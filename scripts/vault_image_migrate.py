"""Move legacy base64 Vault images into encrypted storage.

Rows written before images became files keep a `data:` URL in `og_image`. They
still serve correctly, so this script is optional housekeeping: it shrinks the
database and puts those screenshots under the same file encryption as everything
else.

    uv run python -m scripts.vault_image_migrate            # report only
    uv run python -m scripts.vault_image_migrate --apply    # do it

The report is printed before anything is written, and `--apply` verifies each row
by comparing the decrypted file against the data URL it replaced.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.storage import get_storage
from app.modules.vault.images import IMAGE_MEDIA_TYPES, IMAGE_PREFIX, store_image_bytes
from app.modules.vault.models import VaultItem
from app.modules.vault.services import decode_data_image


async def candidates(session) -> list[VaultItem]:
    result = await session.execute(
        select(VaultItem).where(VaultItem.image_path.is_(None), VaultItem.og_image.is_not(None))
    )
    return [item for item in result.scalars() if decode_data_image(item.og_image)]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the files and clear the column")
    args = parser.parse_args()

    storage = get_storage()
    moved = skipped = failed = 0

    async with AsyncSessionLocal() as session:
        for item in await candidates(session):
            payload, media_type = decode_data_image(item.og_image)
            suffix = IMAGE_MEDIA_TYPES[media_type.lower()]

            if not args.apply:
                # A report must not touch the filesystem. Writing the file and then
                # reporting "nothing was written" is the one lie a migration may
                # not tell, so the dry run only computes the digest it already has.
                skipped += 1
                print(
                    f"  item {item.id}: would move {len(payload)} bytes to {IMAGE_PREFIX}/{item.id}.{suffix}.enc"
                )
                continue

            try:
                path = store_image_bytes(payload, media_type, item.id)
                if storage.get_file_decrypted(path) != payload:
                    raise ValueError("the stored file does not match the original bytes")
            except Exception as error:
                failed += 1
                print(f"  item {item.id}: {error}")
                continue

            item.image_path = path
            item.og_image = None
            moved += 1
            print(f"  item {item.id}: moved {len(payload)} bytes to {path}")

        if args.apply:
            await session.commit()

    print(f"\nmoved {moved}, would move {skipped}, failed {failed}")
    if not args.apply and skipped:
        print("nothing was written; re-run with --apply")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
