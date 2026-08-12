from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    from app.modules.vault.router import get_vault_sync_manifest

    manifest = await get_vault_sync_manifest(db=db, hybrid=False)
    return manifest.get("resources", [])
