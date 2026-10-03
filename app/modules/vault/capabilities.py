from sqlalchemy.ext.asyncio import AsyncSession

VAULT_PACKAGE_ID = "vault_all"


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    if package_id != VAULT_PACKAGE_ID:
        raise ValueError("Invalid Vault package ID")

    from app.modules.vault.router import get_vault_sync_manifest

    manifest = await get_vault_sync_manifest(db=db, hybrid=False)
    return manifest.get("resources", [])
