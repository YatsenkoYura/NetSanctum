from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.video_archiver.models import ArchivedVideo


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    if package_id.startswith("video_playlist_"):
        from app.modules.video_archiver.router import get_playlist_sync_manifest

        playlist_id = int(package_id.removeprefix("video_playlist_"))
        manifest = await get_playlist_sync_manifest(playlist_id, db=db, hybrid=False)
    else:
        from app.modules.video_archiver.router import get_video_sync_manifest

        video_id = package_id.removeprefix("video_")
        manifest = await get_video_sync_manifest(video_id, db=db, hybrid=False)
    return manifest.get("resources", [])


async def resolve_entity(db: AsyncSession, entity_type: str, entity_id: str) -> dict | None:
    video = await db.get(ArchivedVideo, entity_id)
    if not video:
        return None
    return {
        "type": entity_type,
        "title": video.title,
        "url": f"/video-archiver/dashboard?video_id={video.id}",
        "thumbnail": f"/api/video-archiver/videos/{video.id}/thumbnail" if video.thumbnail_path else None,
    }
