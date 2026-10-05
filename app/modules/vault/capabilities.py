from sqlalchemy.ext.asyncio import AsyncSession

VAULT_PACKAGE_ID = "vault_all"


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    if package_id == VAULT_PACKAGE_ID:
        from app.modules.vault.router import get_vault_sync_manifest

        manifest = await get_vault_sync_manifest(db=db, hybrid=False)
        return manifest.get("resources", [])

    from app.modules.vault.sealed_package import (
        collection_id_for_package,
        is_sealed_package_id,
        require_sealed_collection,
        sealed_package_resources,
    )

    if not is_sealed_package_id(package_id):
        raise ValueError("Invalid Vault package ID")
    try:
        collection = await require_sealed_collection(db, collection_id_for_package(package_id))
    except (LookupError, ValueError) as exc:
        raise ValueError(f"Invalid Vault package ID: {package_id!r}") from exc
    return await sealed_package_resources(db, collection)
