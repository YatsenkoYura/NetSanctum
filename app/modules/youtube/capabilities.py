from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.youtube.services import channel_url, playlist_url, video_url


async def resolve_entity(db: AsyncSession, entity_type: str, entity_id: str) -> dict | None:
    if entity_type == "youtube_video":
        source_url = video_url(entity_id)
    elif entity_type == "youtube_playlist":
        source_url = playlist_url(entity_id)
    elif entity_type == "youtube_channel":
        source_url = channel_url(entity_id)
    else:
        return None
    return {
        "type": entity_type,
        "id": entity_id,
        "title": f"YouTube {entity_type.removeprefix('youtube_')}",
        "url": source_url,
        "source_url": source_url,
    }
