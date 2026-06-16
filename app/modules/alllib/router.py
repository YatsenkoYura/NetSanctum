"""
FastAPI router for Lib Network (alllib) module.
"""

import asyncio
import io
import json
import logging
import mimetypes
import re
import urllib.parse
import zipfile

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.scheduler import celery_app
from app.core.security import get_current_user
from app.core.storage import get_storage
from app.core.templates import templates
from app.modules.alllib.epub_builder import EPUBBuilder
from app.modules.alllib.i18n import TRANSLATIONS
from app.modules.alllib.models import LibChapter, LibMedia
from app.modules.alllib.schemas import DownloadRequest
from app.modules.alllib.tasks import download_lib_task

router = APIRouter(prefix="/alllib", tags=["alllib"])
settings = get_settings()
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
logger = logging.getLogger(__name__)


def _get_lang(request: Request) -> str:
    return request.cookies.get("lang") or "en"


def _t(key: str, lang: str = "en") -> str:
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


@router.get("/helper.user.js", include_in_schema=False)
def get_helper_userscript():
    """Return the Tampermonkey helper userscript for direct installation."""
    userscript_content = """// ==UserScript==
// @name         NetSanctum Autofill Helper
// @namespace    http://tampermonkey.net/
// @version      0.1
// @description  Autofills Lib network titles/tokens to NetSanctum
// @match        https://*.mangalib.me/*
// @match        https://*.ranobelib.me/*
// @match        https://*.hentailib.org/*
// @match        https://*.slashlib.me/*
// @match        https://*.comixlib.me/*
// @match        https://*.anilib.me/*
// @grant        none
// ==UserScript==

(function() {
    'use strict';
    if (window.self !== window.top) {
        function sendUpdate() {
            let token = localStorage.getItem('token') || localStorage.getItem('authorization');
            if (!token) {
                for (let i = 0; i < localStorage.length; i++) {
                    let key = localStorage.key(i);
                    if (key && key.toLowerCase().includes('token')) {
                        let val = localStorage.getItem(key);
                        if (val && val.length > 20) { token = val; break; }
                    }
                }
            }
            window.parent.postMessage({
                type: 'netsanctum-nav',
                url: window.location.href,
                token: token
            }, '*');
        }

        sendUpdate();
        let lastUrl = window.location.href;
        new MutationObserver(() => {
            if (window.location.href !== lastUrl) {
                lastUrl = window.location.href;
                sendUpdate();
            }
        }).observe(document, {subtree: true, childList: true});
    }
})();
"""
    return Response(content=userscript_content, media_type="application/javascript")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def alllib_dashboard(
    request: Request,
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Render the primary Lib Network dashboard."""
    return templates.TemplateResponse(request, "alllib_dashboard.html", {"user": user, "lang": lang})


@router.get("/reader/{media_id}", response_class=HTMLResponse, include_in_schema=False)
async def alllib_reader(
    media_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Render the unified premium reader interface (renders text or image mode based on media format)."""
    media = await db.get(LibMedia, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    # Fetch chapters ordered by volume and chapter number
    stmt = (
        select(LibChapter)
        .where(LibChapter.media_id == media_id)
        .order_by(LibChapter.volume_int.asc(), LibChapter.number_float.asc())
    )
    result = await db.execute(stmt)
    chapters = result.scalars().all()

    first_chapter_id = chapters[0].id if chapters else None

    if media.media_type == "anime":
        template_name = "reader_anime.html"
    elif media.media_type == "novel":
        template_name = "reader_novel.html"
    else:
        template_name = "reader_manga.html"

    return templates.TemplateResponse(
        request,
        template_name,
        {
            "user": user,
            "lang": lang,
            "novel": media,
            "media": media,
            "chapters": chapters,
            "first_chapter_id": first_chapter_id,
        },
    )


# ── HTMX UI Partial Renders ──────────────────────────────


@router.get("/ui/library_tab", response_class=HTMLResponse, include_in_schema=False)
async def get_library_tab_ui(
    request: Request,
    response: Response,
    lang: str = Depends(_get_lang),
):
    """HTMX partial: render the library tab search bar, formats selector, and grid wrapper."""
    html = f"""
    <!-- Search and Filter Bar -->
    <div class="bg-zinc-950 border border-zinc-900 p-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div class="flex flex-1 flex-col md:flex-row items-stretch md:items-center gap-3">
            <input type="text" id="library-search" name="search" oninput="alllibApplyFilters()"
                   placeholder="{_t("search_placeholder", lang)}"
                   class="flex-1 bg-black border border-zinc-800 focus:border-teal-400 px-3 py-2 text-xs font-mono text-white focus:outline-none transition-colors">

            <select id="library-format" name="format_filter" onchange="alllibRefreshLibrary(); alllibApplyFilters();"
                    class="bg-black border border-zinc-800 focus:border-teal-400 px-3 py-2 text-xs font-mono text-zinc-400 focus:outline-none transition-colors">
                <option value="all">{_t("filter_all", lang)}</option>
                <option value="all_18plus">{_t("filter_all_18plus", lang)}</option>
                <option value="novel">{_t("type_novel", lang)}</option>
                <option value="manga">{_t("type_manga", lang)}</option>
                <option value="hentai">{_t("type_hentai", lang)}</option>
                <option value="slash">{_t("type_slash", lang)}</option>
                <option value="comics">{_t("type_comics", lang)}</option>
                <option value="anime">{_t("type_anime", lang)}</option>
            </select>
        </div>
    </div>

    <!-- Grid Container -->
    <div id="library-items"
         hx-get="/alllib/ui/library"
         hx-trigger="load"
         hx-include="#library-search, #library-format"
         hx-swap="innerHTML">
        <div class="text-center py-12 font-mono text-xs text-zinc-600">Loading library...</div>
    </div>
    """
    return HTMLResponse(html)


@router.get("/ui/novel/{media_id}", response_class=HTMLResponse, include_in_schema=False)
async def get_novel_detail_ui(
    media_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: render details for a single media item."""
    media = await db.get(LibMedia, media_id)
    if not media:
        return HTMLResponse('<div class="text-red-500 font-mono text-xs p-4">Media item not found.</div>')

    # Fetch chapter count
    ch_count_stmt = select(LibChapter).where(LibChapter.media_id == media_id)
    ch_count_res = await db.execute(ch_count_stmt)
    ch_count = len(ch_count_res.scalars().all())

    # Map format translation
    type_key = f"type_{media.media_type}"
    if media.site_id == 4:
        type_key = "type_hentai"
    elif media.site_id == 2:
        type_key = "type_slash"
    elif media.site_id == 5:
        type_key = "type_comics"
    elif media.site_id == 6:
        type_key = "type_anime"

    format_name = _t(type_key, lang)

    return templates.TemplateResponse(
        request,
        "alllib_detail.html",
        {
            "novel": media,
            "media": media,
            "ch_count": ch_count,
            "format_name": format_name,
            "lang": lang,
            "_t": _t,
        },
    )


@router.get("/ui/library", response_class=HTMLResponse, include_in_schema=False)
async def get_library_ui(
    request: Request,
    search: str | None = None,
    format_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: render downloaded novels/manga library grid."""
    stmt = select(LibMedia)
    if format_filter == "all" or not format_filter:
        # Default: hide 18+ (SlashLib=2, HentaiLib=4)
        stmt = stmt.where(LibMedia.site_id != 2, LibMedia.site_id != 4)
    elif format_filter == "all_18plus":
        pass
    else:
        stmt = stmt.where(LibMedia.media_type == format_filter)

    stmt = stmt.order_by(LibMedia.created_at.desc())

    res = await db.execute(stmt)
    media_items = res.scalars().all()

    html = '<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-6 w-full">'
    empty_hidden = "hidden" if media_items else ""
    html += f'<div id="library-empty-message" class="{empty_hidden} col-span-full text-center py-12 font-mono text-xs text-zinc-500">{_t("no_novels", lang)}</div>'

    for m in media_items:
        ch_count_stmt = select(LibChapter).where(LibChapter.media_id == m.id)
        ch_count_res = await db.execute(ch_count_stmt)
        ch_count = len(ch_count_res.scalars().all())

        cover_url = f"/alllib/api/cover/{m.id}" if m.cover_path else "/static/placeholder.jpg"

        # Determine type badge color and name
        badge_cls = "border-teal-400 text-teal-400"
        type_key = f"type_{m.media_type}"
        if m.site_id == 3:  # Novel
            badge_cls = "border-amber-400 text-amber-400"
        elif m.site_id == 4:  # Hentai
            badge_cls = "border-red-400 text-red-400"
            type_key = "type_hentai"
        elif m.site_id == 2:  # Slash
            badge_cls = "border-purple-400 text-purple-400"
            type_key = "type_slash"
        elif m.site_id == 5:  # Comics
            badge_cls = "border-blue-400 text-blue-400"
            type_key = "type_comics"
        elif m.site_id == 6:  # Anime
            badge_cls = "border-green-400 text-green-400"
            type_key = "type_anime"

        type_badge = f'<span class="text-[8px] uppercase tracking-wider font-mono border px-1 py-0.5 rounded-sm {badge_cls}">{_t(type_key, lang)}</span>'

        # Map site_id to format slug
        fmt_slug = m.media_type
        if m.site_id == 2:
            fmt_slug = "slash"
        elif m.site_id == 4:
            fmt_slug = "hentai"
        elif m.site_id == 5:
            fmt_slug = "comics"
        elif m.site_id == 6:
            fmt_slug = "anime"

        # Escaping quotes to prevent HTML layout break
        safe_title = m.title.replace('"', "&quot;")
        safe_eng = (m.eng_name or "").replace('"', "&quot;")
        safe_rus = (m.rus_name or "").replace('"', "&quot;")

        html += f"""
        <div class="library-card group relative bg-zinc-950/60 border border-zinc-900/80 hover:border-zinc-800 flex flex-col justify-between p-4 transition-all duration-300"
             data-title="{safe_title}"
             data-eng-name="{safe_eng}"
             data-rus-name="{safe_rus}"
             data-site-id="{m.site_id}"
             data-format="{fmt_slug}">
            <!-- Cover image -->
            <button hx-get="/alllib/ui/novel/{m.id}" hx-target="#tab-content-library" hx-swap="innerHTML" class="w-full aspect-[2/3] bg-zinc-950 border border-zinc-800 overflow-hidden relative block hover:border-teal-400/60 transition-colors cursor-pointer text-left">
                <img src="{cover_url}" class="w-full h-full object-cover filter brightness-90 group-hover:brightness-100 group-hover:scale-105 transition-all duration-500" loading="lazy">
            </button>

            <!-- Metadata -->
            <div class="flex-1 flex flex-col justify-between min-w-0 mt-4">
                <div class="space-y-1.5">
                    <div class="flex items-center gap-1.5">{type_badge}</div>
                    <button hx-get="/alllib/ui/novel/{m.id}" hx-target="#tab-content-library" hx-swap="innerHTML" class="text-left cursor-pointer block w-full">
                        <h3 class="text-xs font-bold text-zinc-100 line-clamp-2 hover:text-teal-400 transition-colors" title="{m.title}">{m.title}</h3>
                    </button>
                    <p class="text-[9px] text-zinc-500 font-mono mt-0.5 truncate">{m.eng_name or m.rus_name or ""}</p>
                </div>

                <div class="mt-4">
                    <div class="flex items-center justify-between border-t border-zinc-900/80 pt-2 mb-2">
                        <span class="text-[9px] font-mono text-zinc-500">{ch_count} {_t("chapters_count", lang)}</span>
                    </div>

                    <button hx-get="/alllib/ui/novel/{m.id}" hx-target="#tab-content-library" hx-swap="innerHTML"
                       class="w-full text-center bg-teal-400 text-black border border-teal-400 font-mono font-bold text-[10px] uppercase py-2 transition-all block hover:bg-black hover:text-teal-400 cursor-pointer">
                        {_t("details", lang)}
                    </button>
                </div>
            </div>
        </div>
        """
    html += "</div>"
    return HTMLResponse(html)


@router.get("/ui/chapter/{chapter_id}", response_class=HTMLResponse, include_in_schema=False)
async def get_chapter_ui(
    chapter_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """HTMX partial: render a single chapter content (novel HTML or manga pages)."""
    chapter = await db.get(LibChapter, chapter_id)
    if not chapter:
        return HTMLResponse('<div class="text-red-500 font-mono text-xs p-4">Chapter not found.</div>')

    media = await db.get(LibMedia, chapter.media_id)
    if not media:
        return HTMLResponse('<div class="text-red-500 font-mono text-xs p-4">Media not found.</div>')

    title = f"Vol. {chapter.volume} Chapter {chapter.number}"
    if chapter.name:
        title += f" — {chapter.name}"

    # Determine layout based on media type
    if media.media_type == "novel":
        content = (
            chapter.content_html
            or '<div class="text-center text-zinc-500 text-xs py-8">No content downloaded for this chapter.</div>'
        )
        html = f"""
        <div class="max-w-2xl mx-auto px-4 py-8">
            <div class="border-b border-zinc-800 pb-4 mb-6 text-center">
                <h1 class="text-2xl md:text-3xl font-serif font-bold text-zinc-100">{title}</h1>
            </div>
            <div class="prose prose-invert prose-zinc max-w-none text-zinc-300 leading-relaxed font-sans text-sm space-y-4">
                {content}
            </div>
        </div>
        """
        return HTMLResponse(html)
    elif media.media_type == "anime":
        video_url = (
            f"/alllib/api/video/stream?path={urllib.parse.quote(chapter.video_path)}"
            if chapter.video_path
            else ""
        )
        if not video_url:
            html = f"""
            <div class="w-full mx-auto px-4 py-8 text-center">
                <div class="border-b border-zinc-800 pb-4 mb-6">
                    <h1 class="text-xl md:text-2xl font-bold text-zinc-100 font-sans">{title}</h1>
                </div>
                <div class="text-zinc-500 text-xs py-12 bg-zinc-950 border border-zinc-900 font-mono">
                    No video file downloaded for this episode.
                </div>
            </div>
            """
            return HTMLResponse(html)
        html = f"""
        <div class="w-full mx-auto px-4 py-4">
            <div class="border-b border-zinc-800 pb-4 mb-6 text-center">
                <h1 class="text-xl md:text-2xl font-bold text-zinc-100 font-sans">{title}</h1>
            </div>

            <!-- Custom Video Player wrapper for anime-video-player -->
            <div id="anime-video-container" class="custom-video-player relative w-full aspect-video group bg-black overflow-hidden flex items-center justify-center border border-zinc-800 shadow-2xl">
                <video id="anime-video-player" class="w-full h-full object-contain focus:outline-none" autoplay>
                    <source src="{video_url}" type="video/mp4">
                    Your browser does not support the video tag.
                </video>

                <!-- Big center play button -->
                <button onclick="toggleCustomPlay('anime-video-player')" class="center-play-btn absolute bg-black/60 hover:bg-black/80 text-teal-400 border border-zinc-800 hover:border-teal-400 p-4 rounded-full opacity-0 group-hover:opacity-100 transition-opacity duration-200 focus:outline-none z-10">
                    <svg class="w-6 h-6" fill="currentColor" viewBox="0 0 20 20"><path d="M4.5 3.5v13l11-6.5-11-6.5z"/></svg>
                </button>

                <!-- Custom Control Bar -->
                <div class="custom-controls absolute bottom-0 left-0 right-0 p-3 bg-zinc-950/95 border-t border-zinc-800 flex flex-col gap-2 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity duration-200 z-20">
                    <!-- Progress Bar -->
                    <div onclick="seekCustomPlayer(event, 'anime-video-player')" class="progress-container w-full h-1 bg-zinc-850 hover:h-2 transition-all cursor-pointer relative border border-zinc-800">
                        <div class="progress-bar h-full bg-teal-400 w-0"></div>
                    </div>
                    <!-- Controls Row -->
                    <div class="flex justify-between items-center text-[10px] font-mono text-zinc-300">
                        <div class="flex items-center gap-4">
                            <!-- Play button -->
                            <button onclick="toggleCustomPlay('anime-video-player')" class="play-btn font-bold hover:text-teal-400 transition-colors uppercase p-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"/></svg>
                            </button>
                            <!-- Time -->
                            <div class="time-display text-zinc-500">
                                <span class="curr-time">00:00</span> / <span class="dur-time">00:00</span>
                            </div>
                        </div>
                        <div class="flex items-center gap-4">
                            <!-- Volume Controls -->
                            <div class="flex items-center gap-1.5">
                                <button onclick="toggleMuteCustom('anime-video-player')" class="mute-btn font-bold hover:text-teal-400 transition-colors uppercase p-1">
                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z"/></svg>
                                </button>
                                <input type="range" oninput="changeVolumeCustom(event, 'anime-video-player')" class="vol-slider w-12 accent-teal-400 bg-zinc-800 h-1 appearance-none cursor-pointer" min="0" max="1" step="0.1" value="1">
                            </div>
                            <!-- Settings / Speed -->
                            <div class="relative settings-menu-container">
                                <button onclick="toggleSettingsMenu('anime-video-player')" class="settings-btn font-bold hover:text-teal-400 transition-colors p-1">
                                    <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>
                                </button>
                                <!-- Settings Dropdown -->
                                <div class="settings-dropdown absolute bottom-full right-0 mb-2 bg-zinc-950 border border-zinc-800 py-2 hidden flex-col w-40 text-[10px] shadow-2xl z-30 font-mono">
                                    <!-- Speed Section -->
                                    <div class="px-2 pb-1.5 text-left">
                                        <span class="text-zinc-500 font-bold block uppercase tracking-wider text-[8px] mb-1">Speed</span>
                                        <select onchange="changeSpeedCustom(event, 'anime-video-player')" class="speed-select w-full bg-zinc-900 border border-zinc-800 text-zinc-300 rounded px-1 py-0.5 text-[10px] focus:outline-none">
                                            <option value="0.25">0.25x</option>
                                            <option value="0.5">0.5x</option>
                                            <option value="1" selected>1.0x (Normal)</option>
                                            <option value="1.25">1.25x</option>
                                            <option value="1.5">1.5x</option>
                                            <option value="1.75">1.75x</option>
                                            <option value="2">2.0x</option>
                                        </select>
                                    </div>
                                </div>
                            </div>
                            <!-- Fullscreen button -->
                            <button onclick="toggleFullscreenCustom('anime-video-container')" class="fullscreen-btn font-bold hover:text-teal-400 transition-colors uppercase p-1">
                                <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 8V4h4M4 16v4h4m8-12h4V4m-4 16h4v-4"/></svg>
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        """
        return HTMLResponse(html)
    else:
        pages = chapter.pages_list or []
        pages_json = json.dumps([f"/alllib/api/page?path={urllib.parse.quote(p)}" for p in pages])

        html = f"""
        <div class="space-y-6" id="manga-chapter-container" data-pages='{pages_json}'>
            <div class="border-b border-zinc-800 pb-4 mb-6 text-center">
                <h1 class="text-xl md:text-2xl font-bold text-zinc-100 font-sans">{title}</h1>
            </div>

            <!-- Webtoon continuous vertical scroll -->
            <div id="manga-webtoon-view" class="space-y-4 flex flex-col items-center">
        """
        for page_path in pages:
            encoded_path = urllib.parse.quote(page_path)
            html += f"""
            <div class="w-full max-w-2xl bg-zinc-950/20 border border-zinc-900 overflow-hidden relative shadow-lg">
                <img src="/alllib/api/page?path={encoded_path}" class="manga-page-img w-full h-auto object-contain transition-all duration-300" loading="lazy" />
            </div>
            """

        html += """
            </div>

            <!-- Paginated single page view -->
            <div id="manga-paginated-view" class="hidden flex flex-col items-center gap-4">
                <div class="relative group select-none cursor-pointer max-w-2xl w-full flex justify-center bg-zinc-950/20 border border-zinc-900 shadow-lg" onclick="nextMangaPage(event)">
                    <img id="manga-active-page" class="max-h-[80vh] w-full h-auto object-contain" />
                    <div class="absolute left-0 top-0 bottom-0 w-1/4 hover:bg-white/5 transition-colors flex items-center justify-start pl-4 opacity-0 group-hover:opacity-100" onclick="prevMangaPage(event); event.stopPropagation();">
                        <span class="bg-black/80 text-white p-2 text-xs font-mono border border-zinc-800">PREV</span>
                    </div>
                    <div class="absolute right-0 top-0 bottom-0 w-1/4 hover:bg-white/5 transition-colors flex items-center justify-end pr-4 opacity-0 group-hover:opacity-100">
                        <span class="bg-black/80 text-white p-2 text-xs font-mono border border-zinc-800">NEXT</span>
                    </div>
                </div>

                <div class="flex items-center gap-3 font-mono text-xs mt-2">
                    <button onclick="prevMangaPage(event)" class="px-3 py-1 bg-zinc-900 border border-zinc-800 text-zinc-400 hover:text-white transition-colors cursor-pointer">PREV</button>
                    <span id="manga-page-counter" class="text-zinc-500 font-bold">1 / 1</span>
                    <button onclick="nextMangaPage(event)" class="px-3 py-1 bg-zinc-900 border border-zinc-800 text-zinc-400 hover:text-white transition-colors cursor-pointer">NEXT</button>
                </div>
            </div>
        </div>
        """
        return HTMLResponse(html)


@router.get("/ui/active_downloads", response_class=HTMLResponse, include_in_schema=False)
async def get_active_downloads_ui(
    request: Request,
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: list active download tasks."""
    keys = redis_client.keys("alllib_dl:*")
    tasks = []
    for k in keys:
        try:
            val = redis_client.get(k)
            if val:
                tasks.append(json.loads(val))
        except Exception:
            pass

    if not tasks:
        return HTMLResponse(
            f'<div class="text-center py-8 font-mono text-xs text-zinc-600">{_t("no_active", lang)}</div>'
        )

    html = '<div class="space-y-3">'
    for t in tasks:
        progress_val = t.get("progress", "0%")
        html += f"""
        <div class="border border-zinc-800 bg-zinc-950 p-3 flex flex-col gap-2">
            <div class="flex justify-between items-start">
                <div class="min-w-0">
                    <span class="text-[10px] font-mono text-teal-400 truncate block max-w-[400px]" title="{t.get("title")}">{t.get("title")}</span>
                    <span class="text-[9px] font-mono text-zinc-600 block mt-0.5 truncate max-w-[400px]">{t.get("url")}</span>
                </div>
                <button hx-delete="/alllib/api/tasks/{t.get("task_id")}"
                        hx-swap="outerHTML"
                        class="text-[10px] text-red-500 font-mono hover:text-red-400 font-bold ml-4 cursor-pointer">
                    ❌
                </button>
            </div>
            <div>
                <div class="flex justify-between text-[9px] font-mono text-zinc-500 mb-1">
                    <span>{t.get("status")}</span>
                    <span>{progress_val}</span>
                </div>
                <div class="w-full bg-zinc-900 h-1">
                    <div class="bg-teal-400 h-1" style="width: {progress_val}"></div>
                </div>
            </div>
        </div>
        """
    html += "</div>"
    return HTMLResponse(html)


# ── API Endpoints ────────────────────────────────────────


@router.get("/api/anime-options")
async def get_anime_options(url: str, token: str | None = None, user=Depends(get_current_user)):
    """Fetch available seasons, episode count, and translation teams for an AnimeLib URL."""
    from app.modules.alllib.api import LibAPI

    api = LibAPI(auth_token=token)

    site_id, domain = api.get_site_info_from_url(url)
    if site_id != 6:
        raise HTTPException(status_code=400, detail="Not an AnimeLib URL")

    slug = api.extract_slug_from_url(url)
    if not slug:
        raise HTTPException(status_code=400, detail="Invalid AnimeLib URL format")

    info = await asyncio.to_thread(api.get_novel_info, slug, site_id=site_id, domain=domain)
    if not info:
        raise HTTPException(status_code=400, detail="Failed to fetch anime metadata")

    episodes = await asyncio.to_thread(api.get_novel_chapters, slug, site_id=site_id, domain=domain)
    if not episodes:
        raise HTTPException(status_code=400, detail="No episodes found for this anime")

    # Group by seasons and collect season numbers
    seasons = sorted(
        {str(ep.get("volume", "1")) for ep in episodes}, key=lambda x: int(x) if x.isdigit() else 0
    )

    # Collect unique translation/voiceover teams
    # We query the first episode to see the most common translation teams
    teams = []
    if episodes:
        first_episode = episodes[0]
        try:
            players = await asyncio.to_thread(
                api.get_episode_players, first_episode["id"], site_id=site_id, domain=domain
            )
            for pl in players:
                team_name = pl.get("team", {}).get("name")
                if team_name and team_name not in teams:
                    teams.append(team_name)
        except Exception as e:
            logger.warning(f"Failed to fetch player teams: {e}")

    # If there are many episodes, check the last episode's players as well
    if len(episodes) > 1:
        last_episode = episodes[-1]
        try:
            players_last = await asyncio.to_thread(
                api.get_episode_players, last_episode["id"], site_id=site_id, domain=domain
            )
            for pl in players_last:
                team_name = pl.get("team", {}).get("name")
                if team_name and team_name not in teams:
                    teams.append(team_name)
        except Exception as e:
            logger.warning(f"Failed to fetch last episode players: {e}")

    return {
        "title": info.get("rus_name") or info.get("name") or slug,
        "seasons": seasons,
        "total_episodes": len(episodes),
        "teams": teams,
    }


@router.post("/api/download")
async def trigger_download(
    req: DownloadRequest, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Trigger background download task."""
    task = download_lib_task.delay(
        url=req.url,
        auth_token=req.token,
        seasons=req.seasons,
        episodes_range=req.episodes_range,
        translation_team=req.translation_team,
    )

    data = {
        "task_id": task.id,
        "url": req.url,
        "title": "Resolving...",
        "status": "Queued",
        "progress": "0%",
    }
    redis_client.setex(f"alllib_dl:{task.id}", 86400, json.dumps(data))

    return {"task_id": task.id, "status": "Queued"}


@router.delete("/api/novel/{media_id}", response_class=HTMLResponse)
async def delete_media(
    media_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Delete a media item, its chapters, and associated disk files."""
    media = await db.get(LibMedia, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    # Delete all chapter files
    stmt = select(LibChapter).where(LibChapter.media_id == media_id)
    res = await db.execute(stmt)
    chapters = res.scalars().all()

    def delete_files_sync():
        storage = get_storage()
        if media.cover_path:
            try:
                storage.delete_file(media.cover_path)
            except Exception:
                pass
        for ch in chapters:
            if ch.pages_list:
                for page in ch.pages_list:
                    try:
                        storage.delete_file(page)
                    except Exception:
                        pass

    await asyncio.to_thread(delete_files_sync)

    await db.delete(media)
    await db.commit()

    response.headers["HX-Trigger"] = "reloadLibrary"
    return HTMLResponse(
        f'<div class="col-span-full text-center py-12 font-mono text-xs text-zinc-500">{_t("no_novels", lang)}</div>'
    )


@router.post("/api/novel/{media_id}/sync")
async def sync_media(media_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Re-trigger download to fetch updates."""
    media = await db.get(LibMedia, media_id)
    if not media or not media.source_url:
        raise HTTPException(status_code=400, detail="Cannot synchronize media")

    task = download_lib_task.delay(url=media.source_url, sync_only=True)
    return {"task_id": task.id, "status": "Queued"}


@router.get("/api/novel/{media_id}/export")
async def export_media(media_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Export downloaded media to EPUB (for novels) or CBZ (for manga/hentai/comics)."""
    media = await db.get(LibMedia, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    stmt = (
        select(LibChapter)
        .where(LibChapter.media_id == media_id)
        .order_by(LibChapter.volume_int.asc(), LibChapter.number_float.asc())
    )
    res = await db.execute(stmt)
    chapters = res.scalars().all()

    if media.media_type == "novel":
        # Export as EPUB
        raw_epub_bytes = await asyncio.to_thread(EPUBBuilder.build_epub, media, chapters)
        epub_buffer = io.BytesIO(raw_epub_bytes)
        safe_title = (
            "".join(c for c in media.title if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
        )
        filename = f"{safe_title or media.slug}.epub"

        return StreamingResponse(
            epub_buffer,
            media_type="application/epub+zip",
            headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(filename)}"'},
        )
    else:
        # Export as CBZ
        def build_cbz_sync():
            zip_buffer = io.BytesIO()
            storage = get_storage()

            with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
                for ch in chapters:
                    if ch.pages_list:
                        for page_path in ch.pages_list:
                            if storage.file_exists(page_path):
                                try:
                                    is_enc = page_path.endswith(".enc")
                                    if is_enc:
                                        content = storage.get_file_decrypted(page_path)
                                    else:
                                        with storage.get_file_stream(page_path) as f:
                                            content = f.read()
                                    # Strip .enc from the archive filename
                                    raw_name = page_path.split("/")[-1]
                                    if raw_name.endswith(".enc"):
                                        raw_name = raw_name[:-4]
                                    arc_filename = f"Vol_{ch.volume}_Ch_{ch.number}/{raw_name}"
                                    zip_file.writestr(arc_filename, content)
                                except Exception as e:
                                    logger.error(f"Failed to add page to CBZ: {e}")
            zip_buffer.seek(0)
            return zip_buffer

        zip_buffer = await asyncio.to_thread(build_cbz_sync)
        safe_title = (
            "".join(c for c in media.title if c.isalnum() or c in (" ", "_", "-")).strip().replace(" ", "_")
        )
        filename = f"{safe_title or media.slug}.cbz"

        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )


@router.get("/api/cover/{media_id}", include_in_schema=False)
async def get_cover(media_id: int, db: AsyncSession = Depends(get_db)):
    """Serve cover image from storage backend (auto-decrypts encrypted covers)."""
    media = await db.get(LibMedia, media_id)
    if not media or not media.cover_path:
        return RedirectResponse(url="/static/placeholder.jpg")

    storage = get_storage()
    if not storage.file_exists(media.cover_path):
        return RedirectResponse(url="/static/placeholder.jpg")

    # Transparently decrypt .enc files
    is_encrypted = media.cover_path.endswith(".enc")
    base_path = media.cover_path[:-4] if is_encrypted else media.cover_path
    mime_type, _ = mimetypes.guess_type(base_path)

    if is_encrypted:
        stream = storage.get_file_stream_decrypted(media.cover_path)
    else:
        stream = storage.get_file_stream(media.cover_path)

    return StreamingResponse(stream, media_type=mime_type or "image/jpeg")


@router.get("/api/page", include_in_schema=False)
async def get_page(path: str, user=Depends(get_current_user)):
    """Serve a downloaded page image from the storage backend (auto-decrypts encrypted pages)."""
    storage = get_storage()
    if not storage.file_exists(path):
        raise HTTPException(status_code=404, detail="Page not found")

    # Transparently decrypt .enc files
    is_encrypted = path.endswith(".enc")
    base_path = path[:-4] if is_encrypted else path
    mime_type, _ = mimetypes.guess_type(base_path)

    if is_encrypted:
        stream = storage.get_file_stream_decrypted(path)
    else:
        stream = storage.get_file_stream(path)

    return StreamingResponse(stream, media_type=mime_type or "image/jpeg")


@router.get("/api/video/stream", include_in_schema=False)
async def stream_anime_video(path: str, user=Depends(get_current_user)):
    """Stream anime video file with seek capability."""
    from app.core.storage import LocalStorage

    storage = get_storage()
    if not storage.file_exists(path):
        raise HTTPException(status_code=404, detail="Video file not found")

    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type:
        mime_type = "video/mp4"

    if isinstance(storage, LocalStorage):
        full_path = storage._full_path(path)
        return FileResponse(full_path, media_type=mime_type)

    is_encrypted = path.endswith(".enc")
    if is_encrypted:
        stream = storage.get_file_stream_decrypted(path)
    else:
        stream = storage.get_file_stream(path)

    return StreamingResponse(stream, media_type=mime_type)


@router.get("/api/proxy-image", include_in_schema=False)
async def proxy_image(url: str, user=Depends(get_current_user)):
    """Proxy/cache novel chapter images to bypass WAF referer checks."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://ranobelib.me/",
    }
    try:
        import requests as requests_lib

        resp = await asyncio.to_thread(requests_lib.get, url, headers=headers, timeout=15)
        if resp.status_code == 200:
            mime_type = resp.headers.get("content-type", "image/jpeg")
            return Response(content=resp.content, media_type=mime_type)
        else:
            raise HTTPException(status_code=400, detail="External image load returned non-200")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Proxy error: {e}")


@router.delete("/api/tasks/all")
async def cancel_all_tasks(user=Depends(get_current_user)):
    """Cancel all active downloads."""
    keys = redis_client.keys("alllib_dl:*")
    for k in keys:
        try:
            val = redis_client.get(k)
            if val:
                data = json.loads(val)
                task_id = data.get("task_id")
                if task_id:
                    celery_app.control.revoke(task_id, terminate=True)
            redis_client.delete(k)
        except Exception:
            pass
    return {"message": "All tasks cancelled."}


@router.delete("/api/tasks/{task_id}")
async def cancel_task(
    task_id: str,
    response: Response,
    user=Depends(get_current_user),
):
    """Cancel a specific download task."""
    try:
        celery_app.control.revoke(task_id, terminate=True)
    except Exception:
        pass
    keys = redis_client.keys("alllib_dl:*")
    for k in keys:
        try:
            val = redis_client.get(k)
            if val:
                data = json.loads(val)
                if data.get("task_id") == task_id:
                    redis_client.delete(k)
        except Exception:
            pass
    redis_client.delete(f"alllib_dl:{task_id}")
    response.headers["HX-Trigger"] = "reloadActiveTasks"
    return {"message": f"Task {task_id} cancelled."}


@router.get("/api/novels")
async def get_all_media(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API endpoint: list all downloaded media items (novels, manga, etc.) in JSON."""
    stmt = select(LibMedia).order_by(LibMedia.title.asc())
    res = await db.execute(stmt)
    items = res.scalars().all()
    return [
        {
            "id": m.id,
            "title": m.title,
            "slug": m.slug,
            "cover_path": m.cover_path,
            "type": m.media_type,
            "site_id": m.site_id,
        }
        for m in items
    ]


@router.get("/api/novel/{media_id}")
async def get_media_json(media_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API endpoint: get detailed metadata JSON for a single media item."""
    media = await db.get(LibMedia, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    stmt = (
        select(LibChapter)
        .where(LibChapter.media_id == media_id)
        .order_by(LibChapter.volume_int.asc(), LibChapter.number_float.asc())
    )
    res = await db.execute(stmt)
    chapters = res.scalars().all()

    return {
        "id": media.id,
        "title": media.title,
        "rus_name": media.rus_name,
        "eng_name": media.eng_name,
        "slug": media.slug,
        "description": media.description,
        "cover_url": f"/alllib/api/cover/{media.id}" if media.cover_path else None,
        "source_url": media.source_url,
        "chapters": [
            {
                "id": ch.id,
                "volume": ch.volume,
                "number": ch.number,
                "name": ch.name,
                "pages_count": len(ch.pages_list) if ch.pages_list else 0,
            }
            for ch in chapters
        ],
    }


@router.get("/api/novel/{media_id}/sync-manifest")
async def get_media_sync_manifest(
    media_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API endpoint: Generates a NetOutpost sync manifest for offline caching."""
    media = await db.get(LibMedia, media_id)
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")

    resources = [
        {"url": "/static/tailwind.min.js", "type": "js"},
        {"url": "/static/htmx.min.js", "type": "js"},
        {"url": "/alllib/dashboard", "type": "html"},
        {"url": "/alllib/ui/library", "type": "html"},
        {"url": "/alllib/ui/library_tab", "type": "html"},
        {"url": "/alllib/ui/active_downloads", "type": "html"},
        {"url": f"/alllib/reader/{media_id}", "type": "html"},
        {"url": f"/alllib/ui/novel/{media_id}", "type": "html"},
        {"url": "/alllib/api/novels", "type": "json"},
        {"url": f"/alllib/api/novel/{media_id}", "type": "json"},
    ]

    # Export endpoint is only valid for novels
    if media.media_type == "novel":
        resources.append({"url": f"/alllib/api/novel/{media_id}/export", "type": "binary"})

    if media.cover_path:
        resources.append({"url": f"/alllib/api/cover/{media_id}", "type": "image"})

    stmt = select(LibChapter).where(LibChapter.media_id == media_id)
    res = await db.execute(stmt)
    chapters = res.scalars().all()

    for ch in chapters:
        resources.append({"url": f"/alllib/ui/chapter/{ch.id}", "type": "html"})
        if media.media_type == "novel" and ch.content_html:
            found_urls = re.findall(r'["\'](/alllib/api/proxy-image\?url=[^"\']+)["\']', ch.content_html)
            for url_path in found_urls:
                resources.append({"url": url_path, "type": "image"})
        elif media.media_type == "manga" and ch.pages_list:
            for page_path in ch.pages_list:
                encoded_path = urllib.parse.quote(page_path)
                resources.append({"url": f"/alllib/api/page?path={encoded_path}", "type": "image"})
        elif media.media_type == "anime" and ch.video_path:
            encoded_path = urllib.parse.quote(ch.video_path)
            resources.append({"url": f"/alllib/api/video/stream?path={encoded_path}", "type": "binary"})

    pkg_prefix = media.media_type
    pkg_title = f"{media.media_type.capitalize()}: {media.title}"
    return {
        "package_id": f"{pkg_prefix}_{media_id}",
        "package_title": pkg_title,
        "package_name": pkg_title,
        "title": pkg_title,
        "name": pkg_title,
        "root_url": f"/alllib/reader/{media_id}",
        "resources": resources,
    }


# ── Settings API ──────────────────────────────────────────


@router.get("/ui/settings", response_class=HTMLResponse, include_in_schema=False)
async def get_settings_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Render the alllib settings panel (HTMX partial)."""
    from sqlalchemy import and_

    from app.modules.settings.models import Setting

    result = await db.execute(
        select(Setting.value).where(
            and_(
                Setting.scope == "module",
                Setting.module_name == "alllib",
                Setting.key == "lib_auth_token",
            )
        )
    )
    current_token = result.scalar_one_or_none() or ""
    token_preview = (
        f"…{current_token[-12:]}" if len(current_token) > 12 else ("Set" if current_token else "Not set")
    )
    has_token = bool(current_token.strip())

    html = f"""
    <div class="settings-panel" id="alllib-settings-panel">
      <h3 style="color:var(--accent);margin-bottom:1rem;font-size:.85rem;letter-spacing:.1em;text-transform:uppercase;">
        Lib Network · Auth Settings
      </h3>
      <p style="color:var(--text-secondary);font-size:.82rem;margin-bottom:1.2rem;line-height:1.5;">
        Bearer token required for HentaiLib &amp; SlashLib 18+ content.
        Get it from browser DevTools → Network → any <code>api.cdnlibs.org</code> request → <code>Authorization</code> header.
      </p>
      <form hx-post="/alllib/api/settings" hx-target="#alllib-settings-panel" hx-swap="outerHTML">
        <div class="form-group" style="margin-bottom:.9rem;">
          <label style="font-size:.8rem;color:var(--text-secondary);display:block;margin-bottom:.4rem;">
            Lib Network Bearer Token
            {"<span style='color:#4ade80;margin-left:.5rem;'>✓ Active</span>" if has_token else "<span style='color:#f87171;margin-left:.5rem;'>✗ Not set</span>"}
          </label>
          <textarea name="lib_auth_token" rows="3"
            placeholder="eyJ0eXAiOiJKV1Qi..."
            style="width:100%;background:rgba(0,0,0,.3);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:.6rem;font-size:.75rem;font-family:monospace;resize:vertical;"
          ></textarea>
          <small style="color:var(--text-secondary);font-size:.72rem;">
            Current: <code>{token_preview}</code> · Leave blank to keep existing token · Enter CLEAR to remove
          </small>
        </div>
        <button type="submit" class="btn-primary" style="font-size:.8rem;padding:.5rem 1.2rem;">
          Save Token
        </button>
      </form>
    </div>
    """
    return HTMLResponse(html)


@router.post("/api/settings", response_class=HTMLResponse)
async def save_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Save alllib module settings (auth token)."""
    from sqlalchemy import and_

    from app.modules.settings import service as settings_service
    from app.modules.settings.models import Setting

    form = await request.form()
    token_input = (form.get("lib_auth_token") or "").strip()

    if token_input.upper() == "CLEAR":
        # Delete the token
        result = await db.execute(
            select(Setting).where(
                and_(
                    Setting.scope == "module",
                    Setting.module_name == "alllib",
                    Setting.key == "lib_auth_token",
                )
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            await db.delete(existing)
            await db.commit()
        status_html = "<span style='color:#f87171;'>Token cleared.</span>"
    elif token_input:
        await settings_service.upsert_setting(
            db,
            key="lib_auth_token",
            value=token_input,
            scope="module",
            module_name="alllib",
            description="Bearer token for Lib Network API (HentaiLib/SlashLib auth)",
            value_type="string",
            is_secret=True,
        )
        await db.commit()
        preview = f"…{token_input[-12:]}"
        status_html = f"<span style='color:#4ade80;'>✓ Token saved ({preview})</span>"
    else:
        status_html = "<span style='color:var(--text-secondary);'>No changes.</span>"

    html = f"""
    <div class="settings-panel" id="alllib-settings-panel">
      <h3 style="color:var(--accent);margin-bottom:1rem;font-size:.85rem;letter-spacing:.1em;text-transform:uppercase;">
        Lib Network · Auth Settings
      </h3>
      <p style="color:var(--text-secondary);font-size:.82rem;margin-bottom:1.2rem;line-height:1.5;">
        Bearer token required for HentaiLib &amp; SlashLib 18+ content.
      </p>
      <div style="padding:.8rem;background:rgba(0,0,0,.2);border-radius:4px;margin-bottom:1rem;">
        {status_html}
      </div>
      <button hx-get="/alllib/ui/settings" hx-target="#alllib-settings-panel" hx-swap="outerHTML"
        class="btn-secondary" style="font-size:.8rem;padding:.5rem 1.2rem;">
        ← Back to Settings
      </button>
    </div>
    """
    return HTMLResponse(html)


try:
    from sqlalchemy import update

    from app.modules.storage.router import register_file_deletion_hook, register_module_cleanup_hook

    async def alllib_file_deletion_hook(db: AsyncSession, path: str):
        if path.startswith("alllib/") or "cover" in path:
            stmt = update(LibMedia).where(LibMedia.cover_path == path).values(cover_path=None)
            await db.execute(stmt)

    async def alllib_module_cleanup_hook(db: AsyncSession):
        stmt = update(LibMedia).values(cover_path=None)
        await db.execute(stmt)

    register_file_deletion_hook(alllib_file_deletion_hook)
    register_module_cleanup_hook("alllib", alllib_module_cleanup_hook)
except ImportError:
    pass
