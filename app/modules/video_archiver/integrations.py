"""Library viewer integrations implemented by Video Archiver."""

import redis.asyncio as aioredis
from sqlalchemy import or_, select

from app.contracts.library_viewer_v1 import (
    LibraryItem,
    LibraryRequest,
    LibraryResourceRequest,
    LibraryResult,
)
from app.contracts.video_archive_v1 import ArchiveVideoRequest, ArchiveVideoResult
from app.core.config import get_settings
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationResource,
)
from app.core.task_dispatch import dispatch_tracked_async
from app.core.ytdlp_pipeline import is_youtube_playlist_url
from app.modules.video_archiver.models import ArchivedVideo
from app.modules.video_archiver.providers import PlatformRegistry
from app.modules.video_archiver.tasks import download_video_task

redis_client = aioredis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)


def _serialize_video(video: ArchivedVideo) -> LibraryItem:
    return LibraryItem(
        id=video.id,
        kind="video",
        title=video.title,
        subtitle=video.channel_name,
        description=video.description,
        duration=video.duration,
        playable=bool(video.file_path),
    )


async def library_viewer(
    request: LibraryRequest,
    context: IntegrationContext,
) -> LibraryResult:
    if request.operation in {"catalog", "search"}:
        query = select(ArchivedVideo).where(
            ArchivedVideo.status == "completed", ArchivedVideo.file_path.is_not(None)
        )
        if request.operation == "search":
            search = (request.query or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{search}%"
            query = query.where(
                or_(
                    ArchivedVideo.title.ilike(pattern, escape="\\"),
                    ArchivedVideo.channel_name.ilike(pattern, escape="\\"),
                )
            )
        result = await context.session.execute(
            query.order_by(ArchivedVideo.archived_at.desc()).offset(request.offset).limit(request.limit + 1)
        )
        videos = list(result.scalars())
        return LibraryResult(
            module_id="video_archiver",
            title="Video",
            order=20,
            items=[_serialize_video(video) for video in videos[: request.limit]],
            next_offset=request.offset + request.limit if len(videos) > request.limit else None,
        )

    if not request.item_id:
        raise IntegrationRejectedError("Video ID is required")
    video = await context.session.get(ArchivedVideo, request.item_id)
    if not video:
        raise IntegrationNotFoundError("Video was not found")
    item = _serialize_video(video)
    return LibraryResult(module_id="video_archiver", title="Video", order=20, item=item)


async def resolve_library_resource(
    request: LibraryResourceRequest,
    context: IntegrationContext,
) -> IntegrationResource:
    video = await context.session.get(ArchivedVideo, request.item_id)
    if not video:
        raise IntegrationNotFoundError("Video was not found")
    if not video.file_path:
        raise IntegrationRejectedError("Video file is unavailable")
    return IntegrationResource(
        kind="video",
        title=video.title,
        storage_path=video.file_path,
        duration=video.duration,
    )


async def archive_source_video(
    request: ArchiveVideoRequest,
    context: IntegrationContext,
) -> ArchiveVideoResult:
    entity = await context.registry.resolve_entity(
        request.entity_type,
        request.entity_id,
        context.session,
    )
    if not entity:
        raise IntegrationNotFoundError("Source video was not found")
    source_url = entity.get("source_url")
    if not source_url:
        raise IntegrationRejectedError("Source entity does not provide a video URL")
    if is_youtube_playlist_url(source_url):
        raise IntegrationRejectedError("Archive individual videos from a playlist")
    try:
        provider = PlatformRegistry.require_supported_url(source_url)
    except ValueError as exc:
        raise IntegrationRejectedError(str(exc)) from exc
    task = await dispatch_tracked_async(
        download_video_task,
        redis_client,
        "video_dl",
        {
            "url": source_url,
            "platform": provider.platform_id,
            "title": entity.get("title") or "Resolving URL...",
            "status": "Queued from YouTube",
            "progress": "0%",
        },
        kwargs={"url": source_url, "quality": request.quality},
    )
    return ArchiveVideoResult(
        status="dispatched",
        task_id=task.id,
        platform=provider.platform_id,
        message="Video archive queued",
    )
