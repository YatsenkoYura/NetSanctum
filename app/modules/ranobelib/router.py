"""
FastAPI router for RanobeLib downloader module.
"""

import json
import mimetypes
import urllib.parse

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.storage import get_storage
from app.core.templates import templates
from app.modules.ranobelib.i18n import TRANSLATIONS
from app.modules.ranobelib.models import RanobeChapter, RanobeNovel
from app.modules.ranobelib.schemas import DownloadRequest
from app.modules.ranobelib.tasks import download_ranobe_task

router = APIRouter(prefix="/ranobelib", tags=["ranobelib"])
settings = get_settings()
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _get_lang(request: Request) -> str:
    return request.cookies.get("lang") or "en"


def _t(key: str, lang: str = "en") -> str:
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


# ── UI Pages ─────────────────────────────────────────────


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def ranobe_dashboard(
    request: Request,
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Render the primary RanobeLib dashboard."""
    return templates.TemplateResponse(request, "ranobe_dashboard.html", {"user": user, "lang": lang})


@router.get("/reader/{novel_id}", response_class=HTMLResponse, include_in_schema=False)
async def ranobe_reader(
    novel_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Render the premium novel reader interface."""
    novel = await db.get(RanobeNovel, novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    # Fetch chapters ordered by volume and chapter number
    stmt = (
        select(RanobeChapter)
        .where(RanobeChapter.novel_id == novel_id)
        .order_by(RanobeChapter.volume_int.asc(), RanobeChapter.number_float.asc())
    )
    result = await db.execute(stmt)
    chapters = result.scalars().all()

    first_chapter_id = chapters[0].id if chapters else None

    return templates.TemplateResponse(
        request,
        "ranobe_reader.html",
        {
            "user": user,
            "lang": lang,
            "novel": novel,
            "chapters": chapters,
            "first_chapter_id": first_chapter_id,
        },
    )


# ── HTMX UI Partial Renders ──────────────────────────────


@router.get("/ui/library_tab", response_class=HTMLResponse, include_in_schema=False)
async def get_library_tab_ui(
    request: Request,
    lang: str = Depends(_get_lang),
):
    """HTMX partial: render the library tab search bar and grid wrapper."""
    html = f"""
    <!-- Search -->
    <div class="bg-zinc-950 border border-zinc-900 p-4 flex flex-col md:flex-row md:items-center justify-between gap-4">
        <div class="flex flex-1 flex-col md:flex-row items-stretch md:items-center gap-3">
            <input type="text" id="library-search" name="search" oninput="applyFilters()"
                   placeholder="{_t("search_placeholder", lang)}"
                   class="flex-1 bg-black border border-zinc-800 focus:border-teal-400 px-3 py-2 text-xs font-mono text-white focus:outline-none transition-colors">
        </div>
    </div>

    <!-- Grid Container -->
    <div id="library-items" hx-get="/ranobelib/ui/library" hx-trigger="load" hx-swap="innerHTML">
        <div class="text-center py-12 font-mono text-xs text-zinc-600">Loading library...</div>
    </div>
    """
    return HTMLResponse(html)


@router.get("/ui/novel/{novel_id}", response_class=HTMLResponse, include_in_schema=False)
async def get_novel_detail_ui(
    novel_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: render details for a single novel."""
    novel = await db.get(RanobeNovel, novel_id)
    if not novel:
        return HTMLResponse('<div class="text-red-500 font-mono text-xs p-4">Novel not found.</div>')

    # Fetch chapter count
    ch_count_stmt = select(RanobeChapter).where(RanobeChapter.novel_id == novel_id)
    ch_count_res = await db.execute(ch_count_stmt)
    ch_count = len(ch_count_res.scalars().all())

    return templates.TemplateResponse(
        request,
        "ranobe_detail.html",
        {
            "novel": novel,
            "ch_count": ch_count,
            "lang": lang,
            "_t": _t,
        },
    )


@router.get("/ui/library", response_class=HTMLResponse, include_in_schema=False)
async def get_library_ui(
    request: Request,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: render downloaded novels library grid."""
    stmt = select(RanobeNovel)
    if search:
        stmt = stmt.where(
            RanobeNovel.title.ilike(f"%{search}%") | RanobeNovel.description.ilike(f"%{search}%")
        )

    stmt = stmt.order_by(RanobeNovel.created_at.desc())
    res = await db.execute(stmt)
    novels = res.scalars().all()

    html = '<div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-6">'
    if not novels:
        html += f'<div class="col-span-full text-center py-12 font-mono text-xs text-zinc-500">{_t("no_novels", lang)}</div>'

    for novel in novels:
        # Fetch chapter count
        ch_count_stmt = select(RanobeChapter).where(RanobeChapter.novel_id == novel.id)
        ch_count_res = await db.execute(ch_count_stmt)
        ch_count = len(ch_count_res.scalars().all())

        cover_url = f"/ranobelib/api/cover/{novel.id}" if novel.cover_path else "/static/placeholder.jpg"

        html += f"""
        <div class="group relative bg-zinc-950/60 border border-zinc-900/80 hover:border-zinc-800 flex flex-col justify-between p-4 transition-all duration-300">
            <!-- Cover image -->
            <button hx-get="/ranobelib/ui/novel/{novel.id}" hx-target="#tab-content-library" hx-swap="innerHTML" class="w-full aspect-[2/3] bg-zinc-950 border border-zinc-800 overflow-hidden relative block hover:border-teal-400/60 transition-colors cursor-pointer text-left">
                <img src="{cover_url}" class="w-full h-full object-cover filter brightness-90 group-hover:brightness-100 group-hover:scale-105 transition-all duration-500" loading="lazy">
            </button>

            <!-- Novel Metadata -->
            <div class="flex-1 flex flex-col justify-between min-w-0 mt-4">
                <div class="space-y-1">
                    <button hx-get="/ranobelib/ui/novel/{novel.id}" hx-target="#tab-content-library" hx-swap="innerHTML" class="text-left cursor-pointer block w-full">
                        <h3 class="text-xs font-bold text-zinc-100 line-clamp-2 hover:text-teal-400 transition-colors" title="{novel.title}">{novel.title}</h3>
                    </button>
                    <p class="text-[9px] text-zinc-500 font-mono mt-0.5 truncate">{novel.eng_name or novel.rus_name or ""}</p>
                </div>

                <div class="mt-4">
                    <div class="flex items-center justify-between border-t border-zinc-900/80 pt-2 mb-2">
                        <span class="text-[9px] font-mono text-zinc-500">{ch_count} {_t("chapters_count", lang)}</span>
                    </div>

                    <button hx-get="/ranobelib/ui/novel/{novel.id}" hx-target="#tab-content-library" hx-swap="innerHTML"
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
    """HTMX partial: render single chapter content inside reader."""
    chapter = await db.get(RanobeChapter, chapter_id)
    if not chapter:
        return HTMLResponse('<div class="text-red-500 font-mono text-xs p-4">Chapter not found.</div>')

    title = f"Vol. {chapter.volume} Chapter {chapter.number}"
    if chapter.name:
        title += f" — {chapter.name}"

    html = f"""
    <div class="space-y-6">
        <div class="border-b border-zinc-800 pb-4 mb-6 text-center">
            <h1 class="text-xl md:text-2xl font-bold text-zinc-100 font-sans">{title}</h1>
        </div>
        <div class="chapter-body-content text-zinc-300 font-serif leading-relaxed text-base md:text-lg space-y-4">
            {chapter.content_html or '<p class="text-zinc-500 italic text-center">No content available for this chapter.</p>'}
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
    keys = redis_client.keys("ranobe_dl:*")
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
            '<div class="text-center py-8 font-mono text-xs text-zinc-600">No active downloads</div>'
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
                <button hx-delete="/ranobelib/api/tasks/{t.get("task_id")}"
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


@router.post("/api/download")
async def trigger_download(
    req: DownloadRequest, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Trigger background download task for a novel."""
    task = download_ranobe_task.delay(url=req.url)

    # Initial state in Redis
    data = {
        "task_id": task.id,
        "url": req.url,
        "title": "Resolving URL...",
        "status": "Queued",
        "progress": "0%",
    }
    redis_client.setex(f"ranobe_dl:{task.id}", 86400, json.dumps(data))

    return {"task_id": task.id, "message": "Download task queued successfully."}


@router.delete("/api/novel/{novel_id}", response_class=HTMLResponse)
async def delete_novel(
    novel_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Delete downloaded novel and all its chapters from storage & DB, returning the library tab."""
    novel = await db.get(RanobeNovel, novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    storage = get_storage()
    if novel.cover_path:
        try:
            storage.delete_file(novel.cover_path)
        except Exception:
            pass

    await db.delete(novel)
    await db.commit()

    # Return library tab HTML response to reset UI back to library view
    return await get_library_tab_ui(request, lang)


@router.post("/api/novel/{novel_id}/sync")
async def sync_novel(novel_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Trigger background download task to check for updates and download new chapters."""
    novel = await db.get(RanobeNovel, novel_id)
    if not novel or not novel.source_url:
        raise HTTPException(status_code=404, detail="Novel or source URL not found")

    task = download_ranobe_task.delay(url=novel.source_url)

    data = {
        "task_id": task.id,
        "url": novel.source_url,
        "title": f"Syncing: {novel.title}",
        "status": "Queued",
        "progress": "0%",
    }
    redis_client.setex(f"ranobe_dl:{task.id}", 86400, json.dumps(data))
    return {"task_id": task.id, "message": "Sync task scheduled successfully."}


@router.get("/api/novel/{novel_id}/export")
async def export_novel(novel_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Export all chapters of a novel sequentially into a standard EPUB e-book."""
    from app.modules.ranobelib.epub_builder import EPUBBuilder

    novel = await db.get(RanobeNovel, novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    stmt = (
        select(RanobeChapter)
        .where(RanobeChapter.novel_id == novel_id)
        .order_by(RanobeChapter.volume_int.asc(), RanobeChapter.number_float.asc())
    )
    result = await db.execute(stmt)
    chapters = result.scalars().all()

    # Build EPUB bytes
    epub_bytes = EPUBBuilder.build_epub(novel, chapters)
    filename = f"{novel.slug}.epub"

    return Response(
        content=epub_bytes,
        media_type="application/epub+zip",
        headers={"Content-Disposition": f'attachment; filename="{urllib.parse.quote(filename)}"'},
    )


@router.get("/api/novels")
async def list_novels(
    search: str | None = None, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Lists all archived novels."""
    query = select(RanobeNovel)
    if search:
        query = query.where(
            RanobeNovel.title.ilike(f"%{search}%")
            | RanobeNovel.rus_name.ilike(f"%{search}%")
            | RanobeNovel.eng_name.ilike(f"%{search}%")
        )
    query = query.order_by(RanobeNovel.title.asc())
    res = await db.execute(query)
    novels = res.scalars().all()
    return [
        {
            "id": novel.id,
            "title": novel.title,
            "eng_name": novel.eng_name,
            "rus_name": novel.rus_name,
            "slug": novel.slug,
            "source_url": novel.source_url,
            "cover_path": novel.cover_path,
            "description": novel.description,
        }
        for novel in novels
    ]


@router.get("/api/novel/{novel_id}")
async def get_novel_details(
    novel_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Get details of a single novel, including chapters metadata."""
    novel = await db.get(RanobeNovel, novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    stmt = (
        select(RanobeChapter)
        .where(RanobeChapter.novel_id == novel_id)
        .order_by(RanobeChapter.volume_int.asc(), RanobeChapter.number_float.asc())
    )
    res = await db.execute(stmt)
    chapters = res.scalars().all()

    return {
        "id": novel.id,
        "title": novel.title,
        "eng_name": novel.eng_name,
        "rus_name": novel.rus_name,
        "slug": novel.slug,
        "source_url": novel.source_url,
        "cover_path": novel.cover_path,
        "description": novel.description,
        "chapters": [
            {
                "id": ch.id,
                "volume": ch.volume,
                "number": ch.number,
                "name": ch.name,
            }
            for ch in chapters
        ],
    }


@router.get("/api/novel/{novel_id}/sync-manifest")
async def get_novel_sync_manifest(
    novel_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Generates a NetOutpost sync manifest for a specific novel."""
    novel = await db.get(RanobeNovel, novel_id)
    if not novel:
        raise HTTPException(status_code=404, detail="Novel not found")

    resources = [
        {"url": "/static/tailwind.min.js", "type": "js"},
        {"url": "/static/htmx.min.js", "type": "js"},
        {"url": "/static/placeholder.jpg", "type": "image"},
        {"url": "/ranobelib/dashboard", "type": "html"},
        {"url": "/ranobelib/ui/library", "type": "html"},
        {"url": "/ranobelib/ui/library_tab", "type": "html"},
        {"url": "/ranobelib/ui/active_downloads", "type": "html"},
        {"url": f"/ranobelib/reader/{novel_id}", "type": "html"},
        {"url": f"/ranobelib/ui/novel/{novel_id}", "type": "html"},
        {"url": "/api/novels", "type": "json"},
        {"url": f"/api/novel/{novel_id}", "type": "json"},
        {"url": f"/api/novel/{novel_id}/export", "type": "binary"},
    ]
    if novel.cover_path:
        resources.append({"url": f"/api/cover/{novel_id}", "type": "image"})

    # Fetch all chapters to cache their reading views
    chapters_stmt = select(RanobeChapter).where(RanobeChapter.novel_id == novel_id)
    chapters_res = await db.execute(chapters_stmt)
    chapters = chapters_res.scalars().all()
    for ch in chapters:
        resources.append({"url": f"/ranobelib/ui/chapter/{ch.id}", "type": "html"})

    return {
        "package_id": f"novel_{novel_id}",
        "package_title": f"Novel: {novel.title}",
        "package_name": f"Novel: {novel.title}",
        "title": f"Novel: {novel.title}",
        "name": f"Novel: {novel.title}",
        "root_url": f"/ranobelib/reader/{novel_id}",
        "resources": resources,
    }


@router.get("/api/cover/{novel_id}", include_in_schema=False)
async def get_cover(novel_id: int, db: AsyncSession = Depends(get_db)):
    """Serve cover image from storage backend."""
    novel = await db.get(RanobeNovel, novel_id)
    if not novel or not novel.cover_path:
        return RedirectResponse(url="/static/placeholder.jpg")

    storage = get_storage()
    if not storage.file_exists(novel.cover_path):
        return RedirectResponse(url="/static/placeholder.jpg")

    mime_type, _ = mimetypes.guess_type(novel.cover_path)
    stream = storage.get_file_stream(novel.cover_path)
    return StreamingResponse(stream, media_type=mime_type or "image/jpeg")


@router.get("/api/proxy-image", include_in_schema=False)
async def proxy_image(url: str, user=Depends(get_current_user)):
    """Proxy image requests with RanobeLib headers to bypass hotlinking and CORS blocks."""
    import requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://ranobelib.me/",
        "Origin": "https://ranobelib.me",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        return Response(content=resp.content, media_type=content_type)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Image fetch failed: {e}")


@router.delete("/api/tasks/all")
async def cancel_all_downloads(response: Response, user=Depends(get_current_user)):
    """Cancel all active novel downloads."""
    from app.core.scheduler import celery_app

    try:
        celery_app.control.purge()
    except Exception:
        pass

    keys = redis_client.keys("ranobe_dl:*")
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

    response.headers["HX-Trigger"] = "reloadActiveTasks"
    return {"message": "All downloads cancelled."}


@router.delete("/api/tasks/{task_id}")
async def cancel_single_download(task_id: str, response: Response, user=Depends(get_current_user)):
    """Cancel a single active novel download."""
    from app.core.scheduler import celery_app

    try:
        celery_app.control.revoke(task_id, terminate=True)
    except Exception:
        pass
    keys = redis_client.keys("ranobe_dl:*")
    for k in keys:
        try:
            val = redis_client.get(k)
            if val:
                data = json.loads(val)
                if data.get("task_id") == task_id:
                    redis_client.delete(k)
        except Exception:
            pass
    redis_client.delete(f"ranobe_dl:{task_id}")
    response.headers["HX-Trigger"] = "reloadActiveTasks"
    return {"message": f"Task {task_id} cancelled."}
