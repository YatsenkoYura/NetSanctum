"""Delete Vault collections sealed under the retired scrypt standard.

The old standard was deleted with its data rather than migrated: wrappers
naming any KDF but Argon2id are refused at unlock, so these rows can never
open again. This script removes them — database rows and storage files —
through the module's own `delete_collection`, which deletes nested spaces
first and removes every card's files rather than orphaning them.

    uv run python -m scripts.purge_legacy_vaults            # report only
    uv run python -m scripts.purge_legacy_vaults --apply    # delete

Inside Docker: `docker compose exec web python scripts/purge_legacy_vaults.py --apply`.

A nested space sealed under the current standard is deleted too when its
parent goes: the purge walks from roots, and a child cannot outlive the
space it lives in. This script is temporary — once no pre-Argon2id rows
remain anywhere, it goes away with the last of the old standard.
"""

import argparse
import asyncio

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.modules.vault.models import VaultCollection, VaultItem
from app.modules.vault.services import delete_collection


async def candidates(session) -> list[VaultCollection]:
    result = await session.execute(
        select(VaultCollection).where(
            VaultCollection.parent_id.is_(None),
            VaultCollection.is_encrypted.is_(True),
            (VaultCollection.key_kdf.is_(None)) | (VaultCollection.key_kdf != "argon2id"),
        )
    )
    return list(result.scalars().all())


async def subtree_items(session, root_id: int) -> int:
    seen = [root_id]
    queue = [root_id]
    while queue:
        children = (
            (await session.execute(select(VaultCollection.id).where(VaultCollection.parent_id.in_(queue))))
            .scalars()
            .all()
        )
        queue = [child for child in children if child not in seen]
        seen.extend(queue)
    total = (await session.execute(select(func.count()).where(VaultItem.collection_id.in_(seen)))).scalar()
    return int(total or 0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete the collections and their files")
    args = parser.parse_args()

    removed_cards = removed_spaces = 0
    async with AsyncSessionLocal() as session:
        roots = await candidates(session)
        if not roots:
            print("No pre-Argon2id encrypted collections remain.")
            return 0
        for collection in roots:
            items = await subtree_items(session, collection.id)
            name = collection.public_name or f"workspace {collection.id}"
            if not args.apply:
                print(
                    f"  collection {collection.id} ({name}): "
                    f"key_kdf={collection.key_kdf!r}, ~{items} cards — would delete"
                )
                continue
            result = await delete_collection(session, collection.id)
            removed_cards += result["cards"]
            removed_spaces += result["spaces"] + 1
            print(f"  deleted collection {collection.id} ({name}): {result['cards']} cards")
    if args.apply:
        print(f"Done: {removed_spaces} spaces, {removed_cards} cards.")
    else:
        print("Dry run: nothing was deleted. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
