"""Integration contracts implemented by the Music module."""

import redis.asyncio as aioredis
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.module_types import IntegrationContext, IntegrationRejectedError
from app.core.task_dispatch import dispatch_tracked_async
from app.modules.music.security import validate_music_url
from app.modules.music.tasks import process_youtube_url_task

redis_client = aioredis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)


class ImportEntityAudioRequest(BaseModel):
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)


class ImportEntityAudioResult(BaseModel):
    status: str
    task_id: str
    message: str


async def import_entity_audio(
    request: ImportEntityAudioRequest,
    context: IntegrationContext,
) -> ImportEntityAudioResult:
    """Queue an audio import from an entity exposed by another active module."""
    entity = await context.registry.resolve_entity(
        request.entity_type,
        request.entity_id,
        context.session,
    )
    if not entity:
        raise IntegrationRejectedError("Source entity was not found")

    source_url = entity.get("source_url")
    if not source_url:
        raise IntegrationRejectedError("Source entity does not provide an importable media URL")
    try:
        validate_music_url(source_url, resolve=False)
    except ValueError as exc:
        raise IntegrationRejectedError(str(exc)) from exc

    task = await dispatch_tracked_async(
        process_youtube_url_task,
        redis_client,
        "music_dl",
        {
            "url": source_url,
            "title": entity.get("title") or "Resolving URL...",
            "status": "Queued from integration",
            "progress": "0%",
        },
        args=(source_url,),
    )
    return ImportEntityAudioResult(
        status="dispatched",
        task_id=task.id,
        message="Audio import queued",
    )
