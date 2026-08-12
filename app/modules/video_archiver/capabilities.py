from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.video_archiver.models import ArchivedVideo


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
