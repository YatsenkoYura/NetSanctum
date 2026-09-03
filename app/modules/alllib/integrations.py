"""Library viewer integrations implemented by AllLib."""

from html.parser import HTMLParser

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
from app.modules.alllib.models import LibChapter, LibMedia


class _PlainTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        elif tag in {"p", "div", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n\n".join(line for line in lines if line)


def _serialize_media(media: LibMedia) -> LibraryItem:
    return LibraryItem(
        id=str(media.id),
        kind=media.media_type,
        title=media.title,
        subtitle=media.rus_name or media.eng_name,
        description=media.description,
        playable=media.media_type == "anime",
        readable=media.media_type in {"novel", "manga"},
    )


def _serialize_chapter(chapter: LibChapter, media_type: str) -> LibraryItem:
    return LibraryItem(
        id=str(chapter.id),
        kind="episode" if media_type == "anime" else "chapter",
        title=chapter.name or f"Vol. {chapter.volume}, {chapter.number}",
        subtitle=f"{chapter.volume}:{chapter.number}",
        playable=media_type == "anime" and bool(chapter.video_path),
        readable=(media_type == "novel" and bool(chapter.content_html))
        or (media_type == "manga" and bool(chapter.pages_list)),
        pages_count=len(chapter.pages_list or []),
    )


async def library_viewer(
    request: LibraryRequest,
    context: IntegrationContext,
) -> LibraryResult:
    if request.operation == "catalog":
        result = await context.session.execute(
            select(LibMedia).order_by(LibMedia.title.asc()).offset(request.offset).limit(request.limit + 1)
        )
        media_items = list(result.scalars())
        return LibraryResult(
            module_id="alllib",
            title="Lib Network",
            order=40,
            items=[_serialize_media(media) for media in media_items[: request.limit]],
            next_offset=request.offset + request.limit if len(media_items) > request.limit else None,
        )

    try:
        media_id = int(request.item_id or "")
    except ValueError as exc:
        raise IntegrationRejectedError("Valid media ID is required") from exc
    media = await context.session.get(LibMedia, media_id)
    if not media:
        raise IntegrationNotFoundError("Library item was not found")
    if request.operation == "detail":
        chapters_result = await context.session.execute(
            select(LibChapter)
            .where(LibChapter.media_id == media.id)
            .order_by(LibChapter.volume_int.asc(), LibChapter.number_float.asc())
        )
        chapters = list(chapters_result.scalars())
        item = _serialize_media(media).model_copy(
            update={"children": [_serialize_chapter(ch, media.media_type) for ch in chapters]}
        )
        return LibraryResult(module_id="alllib", title="Lib Network", order=40, item=item)

    raise IntegrationRejectedError("Unsupported library operation")


async def resolve_library_resource(
    request: LibraryResourceRequest,
    context: IntegrationContext,
) -> IntegrationResource:
    try:
        media_id = int(request.item_id)
    except ValueError as exc:
        raise IntegrationRejectedError("Valid media ID is required") from exc
    media = await context.session.get(LibMedia, media_id)
    if not media:
        raise IntegrationNotFoundError("Library item was not found")

    try:
        chapter_id = int(request.child_id or "")
    except ValueError as exc:
        raise IntegrationRejectedError("Valid chapter ID is required") from exc
    chapter = await context.session.get(LibChapter, chapter_id)
    if not chapter or chapter.media_id != media.id:
        raise IntegrationNotFoundError("Chapter was not found")
    if media.media_type == "novel" and chapter.content_html:
        parser = _PlainTextParser()
        parser.feed(chapter.content_html)
        return IntegrationResource(kind="text", title=media.title, text=parser.text())
    if media.media_type == "manga" and chapter.pages_list:
        page = request.page or 0
        if page >= len(chapter.pages_list):
            raise IntegrationRejectedError("Page was not found")
        return IntegrationResource(
            kind="image",
            title=media.title,
            storage_path=chapter.pages_list[page],
            page=page,
            pages_count=len(chapter.pages_list),
        )
    if media.media_type == "anime" and chapter.video_path:
        return IntegrationResource(kind="video", title=media.title, storage_path=chapter.video_path)
    raise IntegrationRejectedError("Chapter content is unavailable")
