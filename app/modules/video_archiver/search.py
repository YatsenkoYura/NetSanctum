from urllib.parse import quote

from sqlalchemy import select

from app.contracts.search_documents_v1 import (
    SearchDocument,
    SearchDocumentsRequest,
    SearchDocumentsResult,
)
from app.core.module_types import IntegrationContext
from app.modules.video_archiver.models import ArchivedVideo


async def search_documents(
    request: SearchDocumentsRequest,
    context: IntegrationContext,
) -> SearchDocumentsResult:
    result = await context.session.execute(
        select(ArchivedVideo)
        .where(ArchivedVideo.status == "completed", ArchivedVideo.file_path.is_not(None))
        .order_by(ArchivedVideo.archived_at.desc(), ArchivedVideo.id.desc())
        .offset(request.offset)
        .limit(request.limit + 1)
    )
    videos = list(result.scalars())
    return SearchDocumentsResult(
        module_id="video_archiver",
        documents=[
            SearchDocument(
                document_id=str(video.id),
                entity_type="video",
                title=video.title[:255],
                subtitle=(video.channel_name or "")[:255] or None,
                body=(video.description or "")[:4000] or None,
                keywords=[str(tag)[:100] for tag in (video.tags or []) if str(tag).strip()][:50],
                updated_at=video.updated_at or video.archived_at,
                open_path=(f"/video-archiver/dashboard?miku_item={quote(str(video.id), safe='')}"),
                playable=True,
            )
            for video in videos[: request.limit]
        ],
        next_offset=request.offset + request.limit if len(videos) > request.limit else None,
    )
