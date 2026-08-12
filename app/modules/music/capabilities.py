from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    if package_id.startswith("song_"):
        from app.modules.music.router import get_song_sync_manifest

        item_id = int(package_id.split("_", 1)[1])
        manifest = await get_song_sync_manifest(item_id, db=db, hybrid=False)
    else:
        from app.modules.music.router import get_playlist_sync_manifest

        item_id = int(package_id.split("_", 1)[1])
        manifest = await get_playlist_sync_manifest(item_id, db=db, hybrid=False)
    return manifest.get("resources", [])
