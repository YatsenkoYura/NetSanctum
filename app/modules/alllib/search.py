import re
from urllib.parse import quote

from sqlalchemy import func, select

from app.contracts.search_documents_v1 import (
    SearchDocument,
    SearchDocumentsRequest,
    SearchDocumentsResult,
)
from app.core.module_types import IntegrationContext
from app.modules.alllib.models import LibChapter, LibMedia

LATIN_TO_CYRILLIC = (
    ("shch", "щ"),
    ("yo", "ё"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ya", "я"),
)
LATIN_CHAR_TO_CYRILLIC = str.maketrans(
    "abvgdeziyklmnoprstufhc",
    "абвгдезийклмнопрстуфхц",
)


def _cyrillic_alias(value: str) -> str:
    alias = value.casefold()
    for latin, cyrillic in LATIN_TO_CYRILLIC:
        alias = alias.replace(latin, cyrillic)
    return alias.translate(LATIN_CHAR_TO_CYRILLIC)


def _media_keywords(media: LibMedia) -> list[str]:
    keywords = []
    if media.media_type == "novel":
        keywords.extend(
            [
                "ранобэ",
                "ранобе",
                "ranobe",
                "новелла",
                "веб новелла",
                "веб-новелла",
                "книга",
                "novel",
                "light novel",
            ]
        )
    elif media.media_type == "manga":
        keywords.extend(["манга", "manga", "комикс", "comic"])
    elif media.media_type == "anime":
        keywords.extend(["аниме", "anime"])

    if media.slug:
        slug_words = [w for w in re.split(r"[-_0-9]+", media.slug) if len(w) >= 2]
        keywords.extend(slug_words)
        keywords.extend(_cyrillic_alias(word) for word in slug_words)

    for name in (media.rus_name, media.eng_name):
        if name and name != media.title:
            keywords.append(name)

    if media.metadata_json and isinstance(media.metadata_json, dict):
        for key in ("authors", "artists", "teams", "genres", "tags", "year", "status"):
            value = media.metadata_json.get(key)
            if isinstance(value, list):
                keywords.extend(str(item)[:100] for item in value if str(item).strip())
            elif value is not None and str(value).strip():
                keywords.append(str(value)[:100])

    return list(dict.fromkeys(keywords))[:50]


async def search_documents(
    request: SearchDocumentsRequest,
    context: IntegrationContext,
) -> SearchDocumentsResult:
    documents = []
    media_count = int(await context.session.scalar(select(func.count(LibMedia.id))) or 0)
    media_offset = min(request.offset, media_count)
    if media_offset < media_count:
        media_result = await context.session.execute(
            select(LibMedia)
            .order_by(LibMedia.title.asc(), LibMedia.id.asc())
            .offset(media_offset)
            .limit(request.limit + 1)
        )
        media_items = list(media_result.scalars())
    else:
        media_items = []
    for media in media_items:
        documents.append(
            SearchDocument(
                document_id=str(media.id),
                entity_type="ranobe" if media.media_type == "novel" else media.media_type,
                title=media.title,
                subtitle=media.rus_name or media.eng_name,
                body=(media.description or "")[:4000] or None,
                keywords=_media_keywords(media),
                updated_at=media.created_at,
                open_path=f"/alllib/reader/{quote(str(media.id), safe='')}",
                readable=True,
            )
        )
    if len(documents) < request.limit + 1:
        chapter_offset = max(0, request.offset - media_count)
        chapter_result = await context.session.execute(
            select(LibChapter, LibMedia)
            .join(LibMedia, LibMedia.id == LibChapter.media_id)
            .order_by(
                LibMedia.title.asc(),
                LibChapter.volume_int.asc(),
                LibChapter.number_float.asc(),
                LibChapter.id.asc(),
            )
            .offset(chapter_offset)
            .limit(request.limit + 1 - len(documents))
        )
        for chapter, media in chapter_result:
            chapter_label = f"Глава {chapter.number}"
            if chapter.volume and chapter.volume != "0":
                chapter_label = f"Том {chapter.volume}, {chapter_label.lower()}"
            chapter_title = f"{media.title} — {chapter_label}"
            if chapter.name:
                chapter_title = f"{chapter_title}: {chapter.name}"
            documents.append(
                SearchDocument(
                    document_id=f"chapter:{chapter.id}",
                    entity_type="ranobe" if media.media_type == "novel" else media.media_type,
                    title=chapter_title[:255],
                    subtitle=media.rus_name or media.eng_name,
                    keywords=_media_keywords(media),
                    updated_at=media.created_at,
                    open_path=(
                        f"/alllib/reader/{quote(str(media.id), safe='')}"
                        f"?chapter={quote(str(chapter.id), safe='')}"
                    ),
                    readable=True,
                )
            )
    page = documents[: request.limit]
    return SearchDocumentsResult(
        module_id="alllib",
        documents=page,
        next_offset=(request.offset + request.limit if len(documents) > request.limit else None),
    )
