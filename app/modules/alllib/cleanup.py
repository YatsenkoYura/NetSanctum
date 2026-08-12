from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.alllib.models import LibMedia


async def cleanup_file(db: AsyncSession, path: str) -> None:
    if path.startswith(("alllib/", "ranobelib/")):
        await db.execute(update(LibMedia).where(LibMedia.cover_path == path).values(cover_path=None))


async def cleanup_module(db: AsyncSession) -> None:
    await db.execute(update(LibMedia).values(cover_path=None))
