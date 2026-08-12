from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.alllib.models import LibMedia


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    from app.modules.alllib.router import get_media_sync_manifest

    media_id = int(package_id.split("_", 1)[1])
    manifest = await get_media_sync_manifest(media_id, db=db, hybrid=False)
    return manifest.get("resources", [])


async def resolve_entity(db: AsyncSession, entity_type: str, entity_id: str) -> dict | None:
    media = await db.get(LibMedia, int(entity_id))
    if not media:
        return None
    return {
        "type": entity_type,
        "title": media.title,
        "url": f"/alllib/reader/{media.id}",
        "thumbnail": f"/alllib/api/cover/{media.id}" if media.cover_path else None,
    }
