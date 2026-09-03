"""Integration contracts implemented by the Music module."""

import redis.asyncio as aioredis
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.contracts.library_viewer_v1 import (
    LibraryItem,
    LibraryRequest,
    LibraryResourceRequest,
    LibraryResult,
)
from app.core.config import get_settings
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationResource,
)
from app.core.task_dispatch import dispatch_tracked_async
from app.modules.music.models import Song
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


def _serialize_song(song: Song) -> LibraryItem:
    return LibraryItem(
        id=str(song.id),
        kind="audio",
        title=song.title,
        subtitle=song.author or song.original_artist or "Unknown artist",
        playable=True,
    )


async def library_viewer(
    request: LibraryRequest,
    context: IntegrationContext,
) -> LibraryResult:
    if request.operation == "catalog":
        result = await context.session.execute(
            select(Song).order_by(Song.created_at.desc()).offset(request.offset).limit(request.limit + 1)
        )
        songs = list(result.scalars())
        return LibraryResult(
            module_id="music",
            title="Music",
            order=10,
            items=[_serialize_song(song) for song in songs[: request.limit]],
            next_offset=request.offset + request.limit if len(songs) > request.limit else None,
        )
    try:
        song_id = int(request.item_id or "")
    except ValueError as exc:
        raise IntegrationRejectedError("Valid song ID is required") from exc
    song = await context.session.get(Song, song_id)
    if not song:
        raise IntegrationNotFoundError("Song was not found")
    item = _serialize_song(song)
    return LibraryResult(module_id="music", title="Music", order=10, item=item)


async def resolve_library_resource(
    request: LibraryResourceRequest,
    context: IntegrationContext,
) -> IntegrationResource:
    try:
        song_id = int(request.item_id)
    except ValueError as exc:
        raise IntegrationRejectedError("Valid song ID is required") from exc
    song = await context.session.get(Song, song_id)
    if not song:
        raise IntegrationNotFoundError("Song was not found")
    return IntegrationResource(
        kind="audio",
        title=song.title,
        subtitle=song.author or song.original_artist or "Unknown artist",
        storage_path=song.audio_file_id,
    )


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
