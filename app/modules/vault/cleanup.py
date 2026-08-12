from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.vault.models import VaultCollection, VaultItem


async def cleanup_module(db: AsyncSession) -> None:
    await db.execute(delete(VaultItem))
    await db.execute(delete(VaultCollection))
