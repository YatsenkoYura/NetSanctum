"""Library viewer integrations implemented by Video Archiver."""

from sqlalchemy import select

from app.contracts.library_viewer_v1 import (
    LibraryItem,
    LibraryRequest,
    LibraryResourceRequest,
    LibraryResult,
)
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationResource,
)
from app.modules.video_archiver.models import ArchivedVideo


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
    if request.operation == "catalog":
        result = await context.session.execute(
            select(ArchivedVideo)
            .where(ArchivedVideo.status == "completed", ArchivedVideo.file_path.is_not(None))
            .order_by(ArchivedVideo.archived_at.desc())
            .offset(request.offset)
            .limit(request.limit + 1)
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
