"""Audit the storage root: who owns what, orphans, and broken references.

Old versions leave files behind: renamed layouts, deleted rows whose files
survived, staging leftovers. The storage browser shows names, not ownership —
this script shows both, so the server owner can see what lives on disk and
why, which is also the security-relevant view (an unknown multi-gigabyte
blob under no module's namespace is a question, not a file).

    uv run python -m scripts.storage_audit                  # report only
    uv run python -m scripts.storage_audit --apply           # delete orphans

Inside Docker: `docker compose exec web python -m scripts.storage_audit`.

What it reports:
- per top-level namespace: owning module (if any), file count, bytes;
- orphan files: on disk under a module namespace but referenced by no
  database row (vault cards are checked against their image/media/thumbnail
  columns; other modules report namespace totals only);
- broken references: database rows pointing at files that are not on disk;
- unowned top-level entries: outside every module namespace — leftovers of
  old versions live here most often.

`--apply` deletes orphan *files* only. It never touches a referenced file,
never touches a directory, and never touches anything outside the storage
root. Directories left empty by deleted orphans are reported, not removed.
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.modules.vault.models import VaultItem


async def vault_referenced_paths(session) -> set[str]:
    paths: set[str] = set()
    for column in (VaultItem.image_path, VaultItem.media_path, VaultItem.media_thumbnail_path):
        rows = (await session.execute(select(column).where(column.is_not(None)))).scalars().all()
        paths.update(str(value) for value in rows if value)
    return paths


def walk_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and not path.is_symlink()),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )


def owner_of(top: str) -> str | None:
    from app.core.modules import module_registry

    return module_registry.storage_owner(top)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="delete orphan files")
    args = parser.parse_args()

    root = Path(get_settings().LOCAL_STORAGE_ROOT).resolve()
    if not root.is_dir():
        print(f"Storage root {root} does not exist.")
        return 1

    files = await asyncio.to_thread(walk_files, root)
    namespaces: dict[str, dict[str, int]] = {}
    for path in files:
        top = path.relative_to(root).parts[0] if len(path.relative_to(root).parts) > 1 else "(root files)"
        entry = namespaces.setdefault(top, {"files": 0, "bytes": 0})
        entry["files"] += 1
        entry["bytes"] += path.stat().st_size

    print(f"Storage root: {root} — {len(files)} files")
    print("\nNamespaces (top-level folder → owning module):")
    for top in sorted(namespaces):
        entry = namespaces[top]
        owner = owner_of(top) if top != "(root files)" else None
        size = entry["bytes"]
        unit = "MiB" if size >= 1024 * 1024 else "KiB"
        shown = size / (1024 * 1024) if unit == "MiB" else size / 1024
        print(f"  {top}/: owner={owner or 'NONE'} files={entry['files']} ~{shown:.1f} {unit}")

    async with AsyncSessionLocal() as session:
        referenced = await vault_referenced_paths(session)

    referenced_rel = set(referenced)
    orphans: list[Path] = []
    for path in files:
        logical = path.relative_to(root).as_posix()
        if logical.split("/", 1)[0] != "vault":
            continue
        if logical not in referenced_rel:
            orphans.append(path)

    print(f"\nVault orphans on disk (no card references them): {len(orphans)}")
    for path in orphans[:20]:
        print(f"  {path.relative_to(root).as_posix()} ({path.stat().st_size} bytes)")
    if len(orphans) > 20:
        print(f"  ... and {len(orphans) - 20} more")

    missing = sorted(
        logical
        for logical in referenced_rel
        if logical.split("/", 1)[0] == "vault" and not (root / logical).is_file()
    )
    print(f"\nDatabase rows pointing at missing files: {len(missing)}")
    for logical in missing[:20]:
        print(f"  {logical}")
    if len(missing) > 20:
        print(f"  ... and {len(missing) - 20} more")

    if not args.apply:
        print("\nDry run: nothing was deleted. Re-run with --apply to delete orphan files.")
        return 0
    removed = 0
    for path in orphans:
        try:
            path.unlink()
            removed += 1
        except OSError as error:
            print(f"  could not delete {path.relative_to(root).as_posix()}: {error}")
    print(f"\nDeleted {removed} orphan files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
