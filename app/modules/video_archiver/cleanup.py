from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.video_archiver.models import ArchivedVideo


async def cleanup_file(db: AsyncSession, path: str) -> None:
    if path.startswith("video_archiver/videos/"):
        await db.execute(delete(ArchivedVideo).where(ArchivedVideo.file_path == path))
    elif path.startswith("video_archiver/thumbnails/"):
        await db.execute(
            update(ArchivedVideo).where(ArchivedVideo.thumbnail_path == path).values(thumbnail_path=None)
        )
    elif path.startswith("video_archiver/subtitles/"):
        result = await db.execute(select(ArchivedVideo))
        for video in result.scalars().all():
            if video.subtitles:
                video.subtitles = {
                    language: subtitle_path
                    for language, subtitle_path in video.subtitles.items()
                    if subtitle_path != path
                }


async def cleanup_module(db: AsyncSession) -> None:
    await db.execute(delete(ArchivedVideo))
