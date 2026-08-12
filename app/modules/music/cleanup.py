from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.music.models import Song


async def cleanup_file(db: AsyncSession, path: str) -> None:
    if path.startswith("music/audio/"):
        await db.execute(delete(Song).where(Song.audio_file_id == path))
    elif path.startswith("music/covers/"):
        await db.execute(update(Song).where(Song.cover_file_id == path).values(cover_file_id=None))


async def cleanup_module(db: AsyncSession) -> None:
    await db.execute(delete(Song))
