from sqlalchemy import select

from app.contracts.vault_spaces_v1 import VaultSpace, VaultSpacesRequest, VaultSpacesResult
from app.core.module_types import IntegrationContext
from app.modules.vault.models import VaultItem
from app.modules.vault.sealing import DEFAULT_SEALED_ALIAS, is_sealed_collection
from app.modules.vault.services import list_collections


def _breadcrumb(
    folder: VaultItem,
    by_id: dict[int, VaultItem],
    collection_names: dict[int, str],
) -> str:
    """Compose a breadcrumb from the folder chain, prefixed with the collection name."""
    chain: list[str] = []
    seen: set[int] = set()
    current: VaultItem | None = folder
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current.title)
        parent_id = current.parent_id
        current = by_id.get(parent_id) if parent_id is not None else None
    chain.reverse()
    prefix = collection_names.get(folder.collection_id or -1)
    if prefix:
        chain.insert(0, prefix)
    return " / ".join(part for part in chain if part)


async def list_spaces(
    request: VaultSpacesRequest,
    context: IntegrationContext,
) -> VaultSpacesResult:
    session = context.session
    spaces: list[VaultSpace] = []

    collections = await list_collections(session)
    # Spaces is consumed by other modules, so a sealed vault appears under its
    # alias and never under its real name.
    collection_names = {
        collection.id: (collection.public_name or DEFAULT_SEALED_ALIAS)
        if is_sealed_collection(collection)
        else collection.name
        for collection in collections
    }
    for collection in collections:
        spaces.append(
            VaultSpace(
                kind="collection",
                id=collection.id,
                name=collection_names[collection.id],
                color=collection.color,
                icon=collection.icon,
                path=collection_names[collection.id],
                items_count=getattr(collection, "items_count", None),
            )
        )

    if request.include_folders:
        statement = select(VaultItem).where(
            VaultItem.is_folder.is_(True),
            # A sealed folder cannot name itself, so it has no place in a tree
            # other modules read.
            VaultItem.sealed_payload.is_(None),
        )
        if not request.include_archived:
            statement = statement.where(VaultItem.is_archived.is_(False))
        if request.collection_id is not None:
            statement = statement.where(VaultItem.collection_id == request.collection_id)
        statement = statement.order_by(VaultItem.title.asc(), VaultItem.id.asc())
        folders = list((await session.execute(statement)).scalars())
        by_id = {folder.id: folder for folder in folders}
        # A parent chain may pass through a folder the filter excluded (archived
        # or from another collection). Fetch the missing links so breadcrumbs
        # never silently truncate.
        missing = {
            folder.parent_id
            for folder in folders
            if folder.parent_id is not None and folder.parent_id not in by_id
        }
        if missing:
            parents = (
                await session.execute(
                    select(VaultItem).where(
                        VaultItem.id.in_(sorted(missing)),
                        VaultItem.is_folder.is_(True),
                    )
                )
            ).scalars()
            for parent in parents:
                by_id.setdefault(parent.id, parent)
        for folder in folders:
            spaces.append(
                VaultSpace(
                    kind="folder",
                    id=folder.id,
                    name=folder.title,
                    color=None,
                    icon=None,
                    path=_breadcrumb(folder, by_id, collection_names) or folder.title,
                    items_count=None,
                )
            )

    spaces.sort(key=lambda space: (0 if space.kind == "collection" else 1, space.path.lower()))
    return VaultSpacesResult(spaces=spaces)


__all__ = ["list_spaces"]
