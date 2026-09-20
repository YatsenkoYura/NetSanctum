from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    if package_id != "storage_manager":
        raise ValueError("Invalid Storage package ID")

    from app.modules.storage.router import get_storage_sync_manifest

    manifest = await get_storage_sync_manifest(user=None, hybrid=False)
    return manifest.get("resources", [])
