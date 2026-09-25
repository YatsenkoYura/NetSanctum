from urllib.parse import quote

from sqlalchemy import select

from app.contracts.search_documents_v1 import (
    SearchDocument,
    SearchDocumentsRequest,
    SearchDocumentsResult,
)
from app.core.module_types import IntegrationContext
from app.modules.music.models import Song


async def search_documents(
    request: SearchDocumentsRequest,
    context: IntegrationContext,
) -> SearchDocumentsResult:
    result = await context.session.execute(
        select(Song)
        .order_by(Song.created_at.desc(), Song.id.desc())
        .offset(request.offset)
        .limit(request.limit + 1)
    )
    songs = list(result.scalars())
    return SearchDocumentsResult(
        module_id="music",
        documents=[
            SearchDocument(
                document_id=str(song.id),
                entity_type="audio",
                title=song.title,
                subtitle=song.author or song.original_artist,
                keywords=[value for value in (song.author, song.original_artist) if value],
                updated_at=song.created_at,
                open_path=f"/music/dashboard?miku_item={quote(str(song.id), safe='')}",
                playable=True,
            )
            for song in songs[: request.limit]
        ],
        next_offset=request.offset + request.limit if len(songs) > request.limit else None,
    )
