import html
import json
import re
import urllib.parse

from fastapi import HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.module_types import ShareAsset, ShareRoute
from app.core.responses import serve_media_stream, serve_storage_file_chunked
from app.core.templates import templates
from app.modules.alllib.i18n import TRANSLATIONS
from app.modules.alllib.models import LibChapter, LibMedia


class AllLibShareProvider:
    async def catalog(self, db: AsyncSession) -> list[dict]:
        result = await db.execute(select(LibMedia).order_by(LibMedia.created_at.desc()))
        return [self._serialize_catalog_item(media) for media in result.scalars().all()]

    async def selection(
        self,
        db: AsyncSession,
        selection_mode: str,
        selector: dict,
    ) -> dict:
        if selection_mode == "all":
            return {}
        if selection_mode != "selected":
            raise HTTPException(status_code=422, detail="Invalid selection mode")

        media_ids = selector.get("media_ids")
        if not isinstance(media_ids, list) or not media_ids:
            raise HTTPException(status_code=422, detail="Select at least one media item")
        if len(media_ids) > 500:
            raise HTTPException(status_code=422, detail="A share may contain at most 500 media items")

        normalized_ids: list[int] = []
        for media_id in media_ids:
            if isinstance(media_id, str) and media_id.isdigit():
                media_id = int(media_id)
            if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id < 1:
                raise HTTPException(status_code=422, detail="Invalid media ID")
            if media_id not in normalized_ids:
                normalized_ids.append(media_id)

        result = await db.execute(select(LibMedia.id).where(LibMedia.id.in_(normalized_ids)))
        found_ids = set(result.scalars().all())
        if missing := set(normalized_ids) - found_ids:
            missing_text = ", ".join(str(media_id) for media_id in sorted(missing))
            raise HTTPException(status_code=422, detail=f"Media not found: {missing_text}")
        return {"media_ids": normalized_ids}

    async def entities(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name == "media":
            return [self._serialize_media(media) for media in await self._selected_media(db, share)]
        if route.name == "media_detail":
            media = await self._get_allowed_media(db, share, self._positive_int(params["media_id"]))
            return self._serialize_media(media)
        if route.name == "library":
            return await self._render_library(request, share, db)
        if route.name == "library_tab":
            return self._render_library_tab(request, share)
        if route.name == "detail":
            media = await self._get_allowed_media(db, share, self._positive_int(params["media_id"]))
            chapter_count = await db.scalar(
                select(func.count(LibChapter.id)).where(LibChapter.media_id == media.id)
            )
            prefix = self._api_prefix(share)
            return templates.TemplateResponse(
                request,
                "alllib_detail.html",
                {
                    "media": media,
                    "novel": media,
                    "ch_count": chapter_count or 0,
                    "format_name": self._media_type_name(media, self._lang(request)),
                    "lang": self._lang(request),
                    "_t": self._translate,
                    "shared_mode": True,
                    "shared_allow_download": share.allow_download,
                    "cover_url": f"{prefix}/media/{media.id}/cover",
                    "detail_back_url": f"{prefix}/library-tab",
                    "reader_url": f"{prefix}/media/{media.id}/reader",
                    "export_url": f"{prefix}/media/{media.id}/export",
                },
            )
        if route.name == "reader":
            media = await self._get_allowed_media(db, share, self._positive_int(params["media_id"]))
            chapters = await self._chapters(db, media.id)
            template_name = {
                "anime": "reader_anime.html",
                "novel": "reader_novel.html",
            }.get(media.media_type, "reader_manga.html")
            return templates.TemplateResponse(
                request,
                template_name,
                {
                    "module_base": "shared_base.html",
                    "shared_mode": True,
                    "share": share,
                    "user": None,
                    "lang": self._lang(request),
                    "novel": media,
                    "media": media,
                    "chapters": chapters,
                    "first_chapter_id": chapters[0].id if chapters else None,
                    "alllib_home_url": f"/s/{share.id}",
                    "chapter_url_prefix": f"{self._api_prefix(share)}/chapters",
                },
            )
        raise HTTPException(status_code=404, detail="Shared entity route not found")

    async def relations(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name == "chapters":
            media = await self._get_allowed_media(db, share, self._positive_int(params["media_id"]))
            return [self._serialize_chapter(chapter) for chapter in await self._chapters(db, media.id)]
        if route.name == "chapter":
            chapter = await self._get_allowed_chapter(
                db,
                share,
                self._positive_int(params["chapter_id"]),
            )
            media = await self._get_allowed_media(db, share, chapter.media_id)
            return self._render_chapter(request, share, media, chapter)
        raise HTTPException(status_code=404, detail="Shared relation route not found")

    async def asset(
        self,
        request: Request,
        share,
        db: AsyncSession,
        asset: ShareAsset,
        params: dict,
    ):
        if asset.name == "cover":
            media = await self._get_allowed_media(db, share, self._positive_int(params["media_id"]))
            if not media.cover_path:
                raise HTTPException(status_code=404, detail="Cover not found")
            response = serve_storage_file_chunked(media.cover_path)
        elif asset.name == "chapter_page":
            chapter = await self._get_allowed_chapter(
                db,
                share,
                self._positive_int(params["chapter_id"]),
            )
            page_paths = self._chapter_page_paths(chapter)
            page_index = self._non_negative_int(params["page_index"])
            if page_index >= len(page_paths):
                raise HTTPException(status_code=404, detail="Page not found")
            response = serve_storage_file_chunked(page_paths[page_index])
        elif asset.name == "chapter_video":
            chapter = await self._get_allowed_chapter(
                db,
                share,
                self._positive_int(params["chapter_id"]),
            )
            if not chapter.video_path:
                raise HTTPException(status_code=404, detail="Video not found")
            response = serve_media_stream(request, chapter.video_path)
        elif asset.name == "export":
            media = await self._get_allowed_media(db, share, self._positive_int(params["media_id"]))
            if not share.allow_download or media.media_type == "anime":
                raise HTTPException(status_code=404, detail="Shared content not found")
            from app.modules.alllib.router import export_media

            response = await export_media(media.id, db, None)
        else:
            raise HTTPException(status_code=404, detail="Shared asset not found")

        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @staticmethod
    def _positive_int(value) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="Shared content not found")
        if normalized < 1:
            raise HTTPException(status_code=404, detail="Shared content not found")
        return normalized

    @staticmethod
    def _non_negative_int(value) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="Shared content not found")
        if normalized < 0:
            raise HTTPException(status_code=404, detail="Shared content not found")
        return normalized

    @staticmethod
    def _is_allowed(share, media_id: int) -> bool:
        return share.selection_mode == "all" or media_id in share.selector.get("media_ids", [])

    async def _get_allowed_media(self, db: AsyncSession, share, media_id: int) -> LibMedia:
        if not self._is_allowed(share, media_id):
            raise HTTPException(status_code=404, detail="Shared content not found")
        media = await db.get(LibMedia, media_id)
        if not media:
            raise HTTPException(status_code=404, detail="Shared content not found")
        return media

    async def _get_allowed_chapter(self, db: AsyncSession, share, chapter_id: int) -> LibChapter:
        chapter = await db.get(LibChapter, chapter_id)
        if not chapter or not self._is_allowed(share, chapter.media_id):
            raise HTTPException(status_code=404, detail="Shared content not found")
        return chapter

    async def _selected_media(self, db: AsyncSession, share) -> list[LibMedia]:
        statement = select(LibMedia).order_by(LibMedia.created_at.desc())
        if share.selection_mode == "all":
            result = await db.execute(statement)
            return list(result.scalars().all())

        media_ids = share.selector.get("media_ids", [])
        result = await db.execute(statement.where(LibMedia.id.in_(media_ids)))
        media_by_id = {media.id: media for media in result.scalars().all()}
        return [media_by_id[media_id] for media_id in media_ids if media_id in media_by_id]

    @staticmethod
    async def _chapters(db: AsyncSession, media_id: int) -> list[LibChapter]:
        result = await db.execute(
            select(LibChapter)
            .where(LibChapter.media_id == media_id)
            .order_by(LibChapter.volume_int.asc(), LibChapter.number_float.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    def _serialize_catalog_item(media: LibMedia) -> dict:
        return {
            "id": media.id,
            "title": media.title,
            "subtitle": media.rus_name or media.eng_name or media.media_type,
            "rus_name": media.rus_name,
            "eng_name": media.eng_name,
            "type": media.media_type,
            "site_id": media.site_id,
            "has_cover": bool(media.cover_path),
            "created_at": media.created_at,
        }

    def _serialize_media(self, media: LibMedia) -> dict:
        return {
            **self._serialize_catalog_item(media),
            "slug": media.slug,
            "description": media.description,
            "source_url": media.source_url,
            "metadata": self._public_metadata(media.metadata_json),
        }

    @staticmethod
    def _serialize_chapter(chapter: LibChapter) -> dict:
        return {
            "id": chapter.id,
            "media_id": chapter.media_id,
            "volume": chapter.volume,
            "number": chapter.number,
            "name": chapter.name,
            "pages_count": len(AllLibShareProvider._chapter_page_paths(chapter)),
            "has_content": bool(chapter.content_html),
            "has_video": bool(chapter.video_path),
        }

    @staticmethod
    def _public_metadata(metadata: dict | None) -> dict:
        if not isinstance(metadata, dict):
            return {}
        allowed = {"rating", "status", "year", "authors", "artists", "teams", "genres", "tags"}
        return {key: metadata[key] for key in allowed if key in metadata}

    @staticmethod
    def _chapter_page_paths(chapter: LibChapter) -> list[str]:
        paths = list(chapter.pages_list or [])
        if chapter.content_html:
            for source in re.findall(r"/alllib/api/page\?path=([^\"'&<> ]+)", chapter.content_html):
                path = urllib.parse.unquote(source)
                if path and path not in paths:
                    paths.append(path)
        return paths

    async def _render_library(self, request: Request, share, db: AsyncSession) -> HTMLResponse:
        media_items = await self._selected_media(db, share)
        format_filter = request.query_params.get("format_filter")
        if format_filter in {None, "", "all"}:
            media_items = [media for media in media_items if media.site_id not in {2, 4}]
        elif format_filter != "all_18plus":
            media_items = [media for media in media_items if self._format_slug(media) == format_filter]

        media_ids = [media.id for media in media_items]
        chapter_counts: dict[int, int] = {}
        if media_ids:
            result = await db.execute(
                select(LibChapter.media_id, func.count(LibChapter.id))
                .where(LibChapter.media_id.in_(media_ids))
                .group_by(LibChapter.media_id)
            )
            chapter_counts = dict(result.all())

        lang = self._lang(request)
        prefix = self._api_prefix(share)
        cards = []
        for media in media_items:
            title = html.escape(media.title, quote=True)
            eng_name = html.escape(media.eng_name or "", quote=True)
            rus_name = html.escape(media.rus_name or "", quote=True)
            display_name = html.escape(media.eng_name or media.rus_name or "")
            cover_url = f"{prefix}/media/{media.id}/cover" if media.cover_path else "/static/placeholder.jpg"
            detail_url = f"{prefix}/media/{media.id}/detail"
            type_name = html.escape(self._media_type_name(media, lang))
            cards.append(
                f"""
                <div class="library-card group relative bg-zinc-950/60 border border-zinc-900/80 hover:border-zinc-800 flex flex-col justify-between p-4 transition-all duration-300"
                     data-title="{title}" data-eng-name="{eng_name}" data-rus-name="{rus_name}"
                     data-site-id="{media.site_id}" data-format="{self._format_slug(media)}">
                    <button hx-get="{detail_url}" hx-target="#tab-content-library" hx-swap="innerHTML"
                            class="w-full aspect-[2/3] bg-zinc-950 border border-zinc-800 overflow-hidden relative block hover:border-teal-400/60 transition-colors cursor-pointer text-left">
                        <img src="{cover_url}" class="w-full h-full object-cover filter brightness-90 group-hover:brightness-100 group-hover:scale-105 transition-all duration-500" loading="lazy">
                    </button>
                    <div class="flex-1 flex flex-col justify-between min-w-0 mt-4">
                        <div class="space-y-1.5">
                            <span class="text-[8px] uppercase tracking-wider font-mono border border-teal-400 text-teal-400 px-1 py-0.5">{type_name}</span>
                            <button hx-get="{detail_url}" hx-target="#tab-content-library" hx-swap="innerHTML" class="text-left cursor-pointer block w-full">
                                <h3 class="text-xs font-bold text-zinc-100 line-clamp-2 hover:text-teal-400 transition-colors" title="{title}">{title}</h3>
                            </button>
                            <p class="text-[9px] text-zinc-500 font-mono mt-0.5 truncate">{display_name}</p>
                        </div>
                        <div class="mt-4 border-t border-zinc-900/80 pt-2">
                            <span class="text-[9px] font-mono text-zinc-500">{chapter_counts.get(media.id, 0)} {self._translate("chapters_count", lang)}</span>
                        </div>
                    </div>
                </div>
                """
            )
        empty_class = "hidden" if cards else ""
        body = (
            '<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-6 w-full">'
            f'<div id="library-empty-message" class="{empty_class} col-span-full text-center py-12 font-mono text-xs text-zinc-500">{self._translate("no_novels", lang)}</div>'
            + "".join(cards)
            + "</div>"
        )
        return HTMLResponse(body)

    def _render_library_tab(self, request: Request, share) -> HTMLResponse:
        lang = self._lang(request)
        prefix = self._api_prefix(share)
        return HTMLResponse(
            f"""
            <div class="bg-zinc-950 border border-zinc-900 p-4 flex flex-col md:flex-row gap-3">
                <input type="text" id="library-search" name="search" oninput="alllibApplyFilters()"
                       placeholder="{html.escape(self._translate("search_placeholder", lang), quote=True)}"
                       class="flex-1 bg-black border border-zinc-800 px-3 py-2 text-xs font-mono text-white">
                <select id="library-format" name="format_filter" onchange="alllibRefreshLibrary(); alllibApplyFilters();"
                        class="bg-black border border-zinc-800 px-3 py-2 text-xs font-mono text-zinc-400">
                    <option value="all">{self._translate("filter_all", lang)}</option>
                    <option value="all_18plus">{self._translate("filter_all_18plus", lang)}</option>
                    <option value="novel">{self._translate("type_novel", lang)}</option>
                    <option value="manga">{self._translate("type_manga", lang)}</option>
                    <option value="hentai">{self._translate("type_hentai", lang)}</option>
                    <option value="slash">{self._translate("type_slash", lang)}</option>
                    <option value="comics">{self._translate("type_comics", lang)}</option>
                    <option value="anime">{self._translate("type_anime", lang)}</option>
                </select>
            </div>
            <div id="library-items" hx-get="{prefix}/library" hx-trigger="load"
                 hx-include="#library-search, #library-format" hx-swap="innerHTML">
                <div class="text-center py-12 font-mono text-xs text-zinc-600">Loading library...</div>
            </div>
            """
        )

    def _render_chapter(
        self,
        request: Request,
        share,
        media: LibMedia,
        chapter: LibChapter,
    ) -> Response:
        title = html.escape(f"Vol. {chapter.volume} Chapter {chapter.number}")
        if chapter.name:
            title += f" - {html.escape(chapter.name)}"
        prefix = self._api_prefix(share)
        if media.media_type == "novel":
            content = self._shared_novel_content(chapter, prefix)
            return HTMLResponse(
                f'<div class="max-w-2xl mx-auto px-4"><h1 class="text-2xl font-serif font-bold text-zinc-100 text-center mb-6">{title}</h1><div class="prose prose-invert prose-zinc max-w-none text-zinc-300 leading-relaxed">{content}</div></div>'
            )
        if media.media_type == "anime":
            if not chapter.video_path:
                return HTMLResponse(
                    '<div class="text-zinc-500 text-xs text-center">No video file downloaded for this episode.</div>'
                )
            video_url = f"{prefix}/chapters/{chapter.id}/video"
            return HTMLResponse(
                f'<div class="custom-video-player relative w-full h-full bg-black"><video id="anime-video-player" class="w-full h-full object-contain" autoplay controls><source src="{video_url}">Your browser does not support the video tag.</video></div>'
            )

        page_urls = [
            f"{prefix}/chapters/{chapter.id}/pages/{index}"
            for index, _path in enumerate(self._chapter_page_paths(chapter))
        ]
        return HTMLResponse(
            '<div id="manga-chapter-container" data-pages="'
            + html.escape(json.dumps(page_urls), quote=True)
            + '"></div>'
        )

    def _shared_novel_content(self, chapter: LibChapter, prefix: str) -> str:
        from app.modules.alllib.ranobehub import sanitize_chapter_html

        content = sanitize_chapter_html(chapter.content_html or "")
        page_paths = self._chapter_page_paths(chapter)

        def replace_image(match: re.Match) -> str:
            tag = match.group(0)
            source_match = re.search(r'src=["\']([^"\']+)["\']', tag)
            if not source_match:
                return ""
            source = source_match.group(1)
            parsed = urllib.parse.urlparse(source)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/alllib/api/page" or not query.get("path"):
                return ""
            try:
                page_index = page_paths.index(query["path"][0])
            except ValueError:
                return ""
            safe_url = f"{prefix}/chapters/{chapter.id}/pages/{page_index}"
            return tag[: source_match.start(1)] + safe_url + tag[source_match.end(1) :]

        content = re.sub(r"<img\b[^>]*>", replace_image, content, flags=re.IGNORECASE)
        return content or '<div class="text-center text-zinc-500 text-xs">No content downloaded.</div>'

    @staticmethod
    def _api_prefix(share) -> str:
        return f"/s/{share.id}/api/alllib"

    @staticmethod
    def _lang(request: Request) -> str:
        return request.cookies.get("lang") or "en"

    @staticmethod
    def _translate(key: str, lang: str = "en") -> str:
        return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(
            key,
            TRANSLATIONS["en"].get(key, key),
        )

    def _media_type_name(self, media: LibMedia, lang: str) -> str:
        type_key = f"type_{self._format_slug(media)}"
        return self._translate(type_key, lang)

    @staticmethod
    def _format_slug(media: LibMedia) -> str:
        return {2: "slash", 4: "hentai", 5: "comics", 6: "anime"}.get(media.site_id) or media.media_type


PROVIDER = AllLibShareProvider()
