import re

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

MUSIC_PACKAGE_PATTERN = re.compile(r"^(song|playlist)_([1-9][0-9]*)$")


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    match = MUSIC_PACKAGE_PATTERN.fullmatch(package_id)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid music package ID")

    package_type, raw_id = match.groups()
    item_id = int(raw_id)
    if package_type == "song":
        from app.modules.music.router import get_song_sync_manifest

        manifest = await get_song_sync_manifest(item_id, db=db, hybrid=False)
    else:
        from app.modules.music.router import get_playlist_sync_manifest

        manifest = await get_playlist_sync_manifest(item_id, db=db, hybrid=False)
    if manifest.get("package_id") != package_id:
        raise HTTPException(status_code=400, detail="Music package identity mismatch")
    return manifest.get("resources", [])
