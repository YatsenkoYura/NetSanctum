from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.alllib.models import LibMedia


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    from fastapi import HTTPException

    from app.modules.alllib.router import _build_media_sync_manifest, _package_media_id

    try:
        media_id = _package_media_id(package_id)
    except HTTPException as exc:
        raise ValueError("Invalid AllLib package id") from exc
    if media_id is None:
        raise ValueError("Invalid AllLib package id")
    media = await db.get(LibMedia, media_id)
    if not media or package_id != f"{media.media_type}_{media_id}":
        raise ValueError("Package id does not match the stored AllLib media type")
    manifest = await _build_media_sync_manifest(
        media_id,
        db,
        hybrid=False,
        create_export_snapshot=False,
    )
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
