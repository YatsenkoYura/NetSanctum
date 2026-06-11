"""
Music module router.
"""

import json
import mimetypes

import redis
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.storage import get_storage
from app.core.templates import templates

redis_client = redis.Redis(host="redis", port=6379, db=0, decode_responses=True)
from app.modules.music.models import Playlist, Song
from app.modules.music.schemas import DownloadRequest
from app.modules.music.tasks import process_youtube_url_task
from app.modules.settings import service as settings_service


def _get_lang(request: Request) -> str:
    return request.cookies.get("lang") or "en"


def _t(key: str, lang: str = "en") -> str:
    from app.modules.music.i18n import TRANSLATIONS

    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


router = APIRouter(prefix="/music", tags=["music"])


@router.get("/api/playlists")
async def api_list_playlists(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Return a list of all playlists."""
    result = await db.execute(select(Playlist).order_by(Playlist.created_at.desc()))
    return [{"id": p.id, "name": p.name, "description": p.description} for p in result.scalars().all()]


@router.get("/api/playlists/{playlist_id}/songs")
async def api_list_playlist_songs(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Return all songs in a specific playlist."""
    from app.modules.music.models import PlaylistSong

    result = await db.execute(
        select(Song)
        .join(PlaylistSong)
        .where(PlaylistSong.playlist_id == playlist_id)
        .order_by(PlaylistSong.position.asc())
    )
    songs = result.scalars().all()
    return [
        {
            "id": s.id,
            "title": s.title,
            "author": s.author,
            "original_artist": s.original_artist,
            "youtube_url": s.youtube_url,
            "audio_url": f"/music/audio/{s.id}",
            "cover_url": f"/music/cover/{s.id}" if s.cover_file_id else None,
        }
        for s in songs
    ]


@router.get("/api/songs")
async def api_list_songs(
    search: str | None = None, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Return a list of all downloaded songs, optionally filtered by search."""
    from sqlalchemy import or_

    query = select(Song).order_by(Song.created_at.desc())
    if search:
        search_term = f"%{search}%"
        query = query.where(
            or_(
                Song.title.ilike(search_term),
                Song.author.ilike(search_term),
                Song.original_artist.ilike(search_term),
            )
        )
    result = await db.execute(query)
    songs = result.scalars().all()
    return [
        {
            "id": s.id,
            "title": s.title,
            "author": s.author,
            "original_artist": s.original_artist,
            "youtube_url": s.youtube_url,
            "audio_url": f"/music/audio/{s.id}",
            "cover_url": f"/music/cover/{s.id}" if s.cover_file_id else None,
        }
        for s in songs
    ]


@router.post("/api/download")
async def api_download(
    req: DownloadRequest, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Trigger YouTube download via Celery."""
    if req.youtube_cookies:
        if req.youtube_cookies.strip().upper() == "CLEAR":
            setting = await settings_service.resolve_setting(db, key="youtube_cookies")
            if setting:
                await settings_service.delete_setting(db, setting.id)
            return {"status": "cleared", "url": req.url, "use_ai": req.use_ai}
        else:
            if (
                "# Netscape HTTP Cookie File" not in req.youtube_cookies
                and ".youtube.com" not in req.youtube_cookies
            ):
                raise HTTPException(status_code=400, detail="Invalid Cookie Format. Must be Netscape format.")

            await settings_service.upsert_setting(
                db,
                key="youtube_cookies",
                value=req.youtube_cookies,
                scope="global",
                value_type="string",
                is_secret=True,
            )
    task = process_youtube_url_task.delay(
        req.url, req.use_ai, req.openai_api_key, req.openai_base_url, req.playlist_id
    )
    data = {
        "task_id": task.id,
        "url": req.url,
        "title": "Resolving URL...",
        "status": "Queued",
        "progress": "0%",
    }
    redis_client.setex(f"music_dl:{task.id}", 86400, json.dumps(data))
    return {"status": "dispatched", "url": req.url, "use_ai": req.use_ai}


@router.get("", response_class=RedirectResponse, include_in_schema=False)
async def redirect_to_music():
    return RedirectResponse(url="/music/dashboard", status_code=303)


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def music_dashboard(
    request: Request,
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Render the full Music dashboard with tabs."""
    return templates.TemplateResponse(request, "music.html", {"user": user, "lang": lang})


@router.get("/ui/player", response_class=HTMLResponse, include_in_schema=False)
async def music_player_ui(
    request: Request,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: list of all downloaded songs."""
    # Template uses only what is in the API!
    songs = await api_list_songs(search=search, db=db, user=user)
    return templates.TemplateResponse(
        request, "player.html", {"songs": songs, "lang": lang, "search": search or ""}
    )


@router.delete("/ui/songs/{song_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_song_ui(song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    song = await db.get(Song, song_id)
    if song:
        await db.delete(song)
        await db.commit()
    return ""


@router.delete(
    "/ui/playlists/{playlist_id}/songs/{song_id}", response_class=HTMLResponse, include_in_schema=False
)
async def remove_song_from_playlist_ui(
    playlist_id: int, song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    from sqlalchemy import select

    from app.modules.music.models import PlaylistSong

    result = await db.execute(
        select(PlaylistSong).where(PlaylistSong.playlist_id == playlist_id, PlaylistSong.song_id == song_id)
    )
    ps = result.scalar_one_or_none()
    if ps:
        await db.delete(ps)
        await db.commit()
    return ""


@router.delete("/ui/playlists/{playlist_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_playlist_ui(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    playlist = await db.get(Playlist, playlist_id)
    if playlist:
        await db.delete(playlist)
        await db.commit()
    return ""


@router.post("/api/playlists", response_class=HTMLResponse, include_in_schema=False)
async def create_playlist_api(
    name: str = Form(...), db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    from app.modules.music.models import Playlist

    playlist = Playlist(name=name)
    db.add(playlist)
    await db.commit()
    # Now we need to return the updated playlists list. We can just return empty and let HTMX hx-get refresh,
    # but wait, the form has hx-get along with hx-post? No, hx-post replaces the area, so we should return the updated list.
    await api_list_playlists(db, user)

    # We can just redirect to the get UI endpoint, or we can use HX-Trigger to trigger a reload.
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = "reloadPlaylists"
    return response


@router.get("/ui/playlists", response_class=HTMLResponse, include_in_schema=False)
async def music_playlists_ui(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: playlists management and download form."""
    # Template uses only what is in the API!
    playlists = await api_list_playlists(db, user)
    return templates.TemplateResponse(request, "playlists.html", {"playlists": playlists, "lang": lang})


@router.get("/ui/playlists/{playlist_id}", response_class=HTMLResponse, include_in_schema=False)
async def music_playlist_detail_ui(
    playlist_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX partial: AIMP-style tracklist for a specific playlist."""
    playlist = await db.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404)
    songs = await api_list_playlist_songs(playlist_id, db, user)
    return templates.TemplateResponse(
        request, "playlist_detail.html", {"playlist": playlist, "songs": songs, "lang": lang}
    )


@router.get("/ui/playlists/{playlist_id}/library", response_class=HTMLResponse, include_in_schema=False)
async def get_playlist_library_ui(
    playlist_id: int,
    request: Request,
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    from sqlalchemy import select

    from app.modules.music.models import PlaylistSong, Song

    lang = _get_lang(request)

    stmt = select(Song)
    if search:
        stmt = stmt.where(Song.title.ilike(f"%{search}%") | Song.author.ilike(f"%{search}%"))
    stmt = stmt.order_by(Song.title)

    res = await db.execute(stmt)
    songs = res.scalars().all()

    in_playlist_stmt = select(PlaylistSong.song_id).where(PlaylistSong.playlist_id == playlist_id)
    in_playlist_res = await db.execute(in_playlist_stmt)
    in_playlist_ids = set(in_playlist_res.scalars().all())

    html = f"""
    <div class="mb-4">
        <input type="text" name="search" value="{search or ""}"
               placeholder="{_t("search_library_placeholder", lang)}"
               hx-get="/music/ui/playlists/{playlist_id}/library"
               hx-trigger="keyup changed delay:200ms"
               hx-target="#playlist-library-sidebar"
               class="w-full bg-black border border-zinc-800 rounded-none px-3 py-1.5 text-xs font-mono text-zinc-100 placeholder-zinc-700 focus:border-teal-400 focus:outline-none transition-colors">
    </div>
    <div class="space-y-2 overflow-y-auto max-h-[400px] pr-2">
    """
    if not songs:
        html += f'<div class="text-xs font-mono text-zinc-600 text-center py-4">{_t("no_songs_in_library", lang)}</div>'
    for song in songs:
        is_added = song.id in in_playlist_ids
        btn = (
            f'<span class="text-[10px] font-mono text-zinc-500 bg-zinc-900 border border-zinc-800 px-2 py-0.5">{_t("in_list", lang)}</span>'
            if is_added
            else f"""
        <button hx-post="/music/ui/playlists/{playlist_id}/songs/{song.id}"
                hx-target="this"
                hx-swap="outerHTML"
                class="text-[10px] font-mono text-emerald-400 border border-emerald-500/30 bg-emerald-950/20 px-2 py-0.5 hover:bg-emerald-500 hover:text-black transition-all">
            {_t("add", lang)}
        </button>
        """
        )

        cover_html = ""
        cover_url = f"/music/cover/{song.id}" if song.cover_file_id else None
        if cover_url:
            cover_html = (
                f'<img src="{cover_url}" class="w-6 h-6 object-cover rounded-sm border border-zinc-800">'
            )
        else:
            cover_html = """
            <div class="w-6 h-6 rounded-sm bg-zinc-900 border border-zinc-800 flex items-center justify-center">
                <svg class="w-3 h-3 text-zinc-700" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"></path></svg>
            </div>
            """

        html += f"""
        <div class="flex items-center gap-3 p-2 bg-zinc-950/40 border border-zinc-900/60 hover:border-zinc-800">
            {cover_html}
            <div class="flex-1 min-w-0">
                <div class="text-xs font-semibold text-zinc-200 truncate" title="{song.title}">{song.title}</div>
                <div class="text-[10px] text-zinc-500 truncate">{song.author or "Unknown"}</div>
            </div>
            <div class="flex-shrink-0">
                {btn}
            </div>
        </div>
        """
    html += "</div>"
    return HTMLResponse(html)


@router.post(
    "/ui/playlists/{playlist_id}/songs/{song_id}", response_class=HTMLResponse, include_in_schema=False
)
async def add_song_to_playlist_ui(
    playlist_id: int,
    song_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    from sqlalchemy import func, select

    from app.modules.music.models import PlaylistSong

    lang = _get_lang(request)

    exists_stmt = select(PlaylistSong).where(
        PlaylistSong.playlist_id == playlist_id, PlaylistSong.song_id == song_id
    )
    exists_res = await db.execute(exists_stmt)
    if exists_res.scalar_one_or_none():
        return HTMLResponse(f'<span class="text-zinc-500 font-mono text-[10px]">{_t("in_list", lang)}</span>')

    pos_stmt = select(func.coalesce(func.max(PlaylistSong.position), -1)).where(
        PlaylistSong.playlist_id == playlist_id
    )
    pos_res = await db.execute(pos_stmt)
    max_pos = pos_res.scalar()
    if max_pos is None:
        max_pos = -1

    ps = PlaylistSong(playlist_id=playlist_id, song_id=song_id, position=max_pos + 1)
    db.add(ps)
    await db.commit()

    response = HTMLResponse(
        f'<span class="text-emerald-400 font-bold font-mono text-[10px]">{_t("added", lang)}</span>'
    )
    response.headers["HX-Trigger"] = "reloadPlaylistDetail"
    return response


@router.post("/ui/download", response_class=HTMLResponse, include_in_schema=False)
async def start_download(
    request: Request,
    url: str = Form(...),
    use_ai: bool = Form(False),
    openai_api_key: str | None = Form(None),
    openai_base_url: str | None = Form(None),
    youtube_cookies: str | None = Form(None),
    playlist_id: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """HTMX endpoint to trigger YouTube download via Celery."""
    if not url:
        return HTMLResponse(
            '<div class="text-red-500 font-mono text-xs border border-red-500 p-2">URL required.</div>'
        )

    # Template uses only what is in the API!
    req_model = DownloadRequest(
        url=url,
        use_ai=use_ai,
        openai_api_key=openai_api_key or None,
        openai_base_url=openai_base_url or None,
        youtube_cookies=youtube_cookies or None,
        playlist_id=playlist_id,
    )
    try:
        await api_download(req_model, db, user)
    except HTTPException as e:
        return HTMLResponse(
            f'<div class="text-red-500 font-mono text-xs border border-red-500 p-2">{e.detail}</div>'
        )

    success_msg = "Download task dispatched!" if lang == "en" else "Задача скачивания запущена!"

    html = f"""
    <div class="border border-emerald-500 bg-emerald-950/20 p-3 rounded-none mb-4">
        <span class="text-emerald-500 text-xs font-mono font-bold uppercase">{success_msg}</span>
    </div>
    """
    return HTMLResponse(html)


@router.get("/ui/downloads_active", response_class=HTMLResponse, include_in_schema=False)
async def active_downloads_ui(request: Request, user=Depends(get_current_user)):
    """HTMX endpoint to poll active downloads."""
    keys = redis_client.keys("music_dl:*")
    downloads = []
    for k in keys:
        data = redis_client.get(k)
        if data:
            downloads.append(json.loads(data))

    if not downloads:
        return HTMLResponse('<div class="text-xs font-mono text-zinc-600">No active downloads</div>')

    html = '<div class="space-y-2">'
    for d in downloads:
        html += f'''
        <div class="border border-zinc-800 bg-zinc-950 p-2 flex flex-col gap-1">
            <div class="flex justify-between items-center">
                <span class="text-[10px] font-mono text-emerald-400 truncate max-w-[200px]" title="{d.get("title")}">{d.get("title")}</span>
                <button hx-delete="/music/ui/downloads/{d.get("task_id")}" hx-swap="outerHTML" class="text-red-500 hover:text-red-400 font-bold ml-2">❌</button>
            </div>
            <div class="flex justify-between items-center text-[9px] font-mono text-zinc-500">
                <span>{d.get("status")}</span>
                <span>{d.get("progress")}</span>
            </div>
        </div>
        '''
    html += "</div>"
    return HTMLResponse(html)


@router.delete("/ui/downloads/all", response_class=HTMLResponse, include_in_schema=False)
async def cancel_all_downloads_ui(request: Request, user=Depends(get_current_user)):
    """HTMX endpoint to cancel all active downloads."""
    from app.core.scheduler import celery_app

    # 1. Purge the queue (drops all non-prefetched tasks)
    celery_app.control.purge()

    # 2. Revoke and terminate all tasks tracked in Redis
    keys = redis_client.keys("music_dl:*")
    for k in keys:
        data = redis_client.get(k)
        if data:
            import json

            parsed = json.loads(data)
            task_id = parsed.get("task_id")
            if task_id:
                celery_app.control.revoke(task_id, terminate=True)
        redis_client.delete(k)
    return HTMLResponse('<div class="text-xs font-mono text-zinc-600">No active downloads</div>')


@router.delete("/ui/downloads/{task_id}", response_class=HTMLResponse, include_in_schema=False)
async def cancel_download_ui(task_id: str, request: Request, user=Depends(get_current_user)):
    """HTMX endpoint to cancel an active download."""
    from app.core.scheduler import celery_app

    celery_app.control.revoke(task_id, terminate=True)
    redis_client.delete(f"music_dl:{task_id}")
    return HTMLResponse("")


@router.get("/api/songs/{song_id}/sync-manifest")
async def get_song_sync_manifest(
    song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Generates a NetOutpost sync manifest for a specific song."""
    song = await db.get(Song, song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    resources = [
        {"url": "/music/api/songs", "type": "json"},
        {"url": f"/music/audio/{song_id}", "type": "binary"},
    ]
    if song.cover_file_id:
        resources.append({"url": f"/music/cover/{song_id}", "type": "image"})

    return {"package_id": f"song_{song_id}", "root_url": "/music/dashboard", "resources": resources}


@router.get("/api/playlists/{playlist_id}/sync-manifest")
async def get_playlist_sync_manifest(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Generates a NetOutpost sync manifest for an entire playlist."""
    playlist = await db.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    from app.modules.music.models import PlaylistSong

    result = await db.execute(
        select(Song)
        .join(PlaylistSong)
        .where(PlaylistSong.playlist_id == playlist_id)
        .order_by(PlaylistSong.position.asc())
    )
    songs = result.scalars().all()

    resources = [
        {"url": "/music/api/playlists", "type": "json"},
        {"url": f"/music/api/playlists/{playlist_id}/songs", "type": "json"},
    ]
    for song in songs:
        resources.append({"url": f"/music/audio/{song.id}", "type": "binary"})
        if song.cover_file_id:
            resources.append({"url": f"/music/cover/{song.id}", "type": "image"})

    return {"package_id": f"playlist_{playlist_id}", "root_url": "/music/dashboard", "resources": resources}


# ── Shared Media Endpoints ───────────────────────────────


@router.get("/audio/{song_id}")
async def get_audio(song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Stream audio file from storage (supports cookie and bearer token)."""
    song = await db.get(Song, song_id)
    if not song or not song.audio_file_id:
        raise HTTPException(status_code=404, detail="Audio not found")

    storage = get_storage()
    if not storage.file_exists(song.audio_file_id):
        raise HTTPException(status_code=404, detail="File missing from storage")

    mime_type, _ = mimetypes.guess_type(song.audio_file_id)

    from fastapi.responses import FileResponse

    from app.core.storage import LocalStorage

    if isinstance(storage, LocalStorage):
        full_path = storage._full_path(song.audio_file_id)
        return FileResponse(full_path, media_type=mime_type or "audio/mpeg")

    def iterfile():
        with storage.get_file_stream(song.audio_file_id) as f:
            while chunk := f.read(8192):
                yield chunk

    return StreamingResponse(iterfile(), media_type=mime_type or "audio/mpeg")


@router.get("/cover/{song_id}")
async def get_cover(song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Serve cover image from storage."""
    song = await db.get(Song, song_id)
    if not song or not song.cover_file_id:
        raise HTTPException(status_code=404, detail="Cover not found")

    storage = get_storage()
    if not storage.file_exists(song.cover_file_id):
        raise HTTPException(status_code=404, detail="File missing from storage")

    mt, _ = mimetypes.guess_type(song.cover_file_id)
    with storage.get_file_stream(song.cover_file_id) as f:
        content = f.read()
    return Response(content=content, media_type=mt or "image/jpeg")
