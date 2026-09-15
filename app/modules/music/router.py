"""
Music module router.
"""

import json
import re

import anyio
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.browser_client import revoke_browser_credentials
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.task_dispatch import dispatch_tracked_async, is_terminal_task_payload
from app.core.templates import templates

redis_client = aioredis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
from app.modules.music.models import Playlist, Song
from app.modules.music.schemas import DownloadRequest
from app.modules.music.security import validate_music_url
from app.modules.music.tasks import process_youtube_url_task
from app.modules.settings import service as settings_service


def _get_lang(request: Request) -> str:
    return request.cookies.get("lang") or "en"


def _t(key: str, lang: str = "en") -> str:
    from app.modules.music.i18n import TRANSLATIONS

    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(key, TRANSLATIONS["en"].get(key, key))


router = APIRouter(prefix="/music", tags=["music"])
TASK_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


async def _regenerate_playlist_cover(db: AsyncSession, playlist: Playlist) -> None:
    from app.modules.music.covers import regenerate_playlist_cover
    from app.modules.music.models import PlaylistSong

    paths = (
        (
            await db.execute(
                select(Song.cover_file_id)
                .join(PlaylistSong)
                .where(PlaylistSong.playlist_id == playlist.id, Song.cover_file_id.isnot(None))
                .order_by(PlaylistSong.position)
                .limit(9)
            )
        )
        .scalars()
        .all()
    )
    playlist.cover_path = await anyio.to_thread.run_sync(regenerate_playlist_cover, playlist, paths)


def _music_package_scope(package_id: str | None) -> tuple[str | None, int | None]:
    if not package_id:
        return None, None
    for scope in ("song", "playlist"):
        prefix = f"{scope}_"
        if package_id.startswith(prefix):
            raw_id = package_id.removeprefix(prefix)
            if raw_id.isdigit() and int(raw_id) > 0:
                return scope, int(raw_id)
    raise HTTPException(status_code=400, detail="Invalid music package ID")


@router.get("/api/playlists")
async def api_list_playlists(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    package_id: str | None = None,
):
    """API: Return a list of all playlists with generated cover URLs."""
    from sqlalchemy.orm import selectinload

    from app.modules.music.models import PlaylistSong

    scope, item_id = _music_package_scope(package_id)
    if scope == "song":
        return []
    query = (
        select(Playlist)
        .options(
            selectinload(Playlist.playlist_songs).selectinload(PlaylistSong.song),
        )
        .order_by(Playlist.created_at.desc())
    )
    if scope == "playlist":
        query = query.where(Playlist.id == item_id)
    result = await db.execute(query)
    playlists = result.scalars().all()
    generated_covers = False
    for playlist in playlists:
        if not playlist.cover_path:
            await _regenerate_playlist_cover(db, playlist)
            generated_covers = True
    if generated_covers:
        await db.commit()

    out = []
    for p in playlists:
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "source_url": p.source_url,
                "cover_url": f"/music/playlists/{p.id}/cover" if p.cover_path else None,
                "songs": [ps.song_id for ps in p.playlist_songs] if p.playlist_songs else [],
            }
        )
    return out


@router.put("/api/playlists/{playlist_id}/source")
async def set_playlist_source(
    playlist_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    body = await request.json()
    source_url = str(body.get("source_url") or "").strip()
    try:
        validate_music_url(source_url, resolve=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    playlist = await db.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    playlist.source_url = source_url
    await db.commit()
    return {"status": "saved", "playlist_id": playlist.id, "source_url": playlist.source_url}


@router.post("/api/playlists/{playlist_id}/sync")
async def sync_playlist_source(
    playlist_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Refresh a source playlist and queue only tracks absent from this playlist."""
    playlist = await db.get(Playlist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if not playlist.source_url:
        raise HTTPException(status_code=400, detail="Attach a playlist URL before synchronizing")
    task = await dispatch_tracked_async(
        process_youtube_url_task,
        redis_client,
        "music_dl",
        {
            "url": playlist.source_url,
            "title": playlist.name,
            "status": "Synchronizing playlist",
            "progress": "0%",
        },
        args=(playlist.source_url, True, None, None, playlist.id),
    )
    return {"status": "dispatched", "task_id": task.id, "playlist_id": playlist.id}


@router.get("/api/playlists/{playlist_id}/songs")
async def api_list_playlist_songs(
    playlist_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    package_id: str | None = None,
):
    """API: Return all songs in a specific playlist."""
    from app.modules.music.models import PlaylistSong

    scope, item_id = _music_package_scope(package_id)
    if (scope != "playlist" and scope is not None) or (scope == "playlist" and item_id != playlist_id):
        raise HTTPException(status_code=404, detail="Playlist is not part of this package")
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
            "cover_offset_x": s.cover_offset_x,
            "cover_offset_y": s.cover_offset_y,
        }
        for s in songs
    ]


@router.get("/api/songs")
async def api_list_songs(
    search: str | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    package_id: str | None = None,
):
    """API: Return a list of all downloaded songs, optionally filtered by search."""
    from sqlalchemy import or_

    scope, item_id = _music_package_scope(package_id)
    query = select(Song).order_by(Song.created_at.desc())
    if scope == "song":
        query = query.where(Song.id == item_id)
    elif scope == "playlist":
        from app.modules.music.models import PlaylistSong

        query = query.join(PlaylistSong).where(PlaylistSong.playlist_id == item_id)
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
            "cover_offset_x": s.cover_offset_x,
            "cover_offset_y": s.cover_offset_y,
        }
        for s in songs
    ]


@router.put("/api/songs/{song_id}/cover-position")
async def update_cover_position(
    song_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """API: Update the cover image crop position for a song."""
    body = await request.json()
    offset_x = float(body.get("offset_x", 50))
    offset_y = float(body.get("offset_y", 50))
    offset_x = max(0, min(100, offset_x))
    offset_y = max(0, min(100, offset_y))

    song = await db.get(Song, song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")
    song.cover_offset_x = offset_x
    song.cover_offset_y = offset_y
    await db.commit()
    return {"status": "ok", "offset_x": offset_x, "offset_y": offset_y}


@router.put("/api/songs/{song_id}")
async def update_song_metadata(
    song_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """API: Quick edit song title, author, or original_artist."""
    body = await request.json()
    song = await db.get(Song, song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    if "title" in body and body["title"].strip():
        song.title = body["title"].strip()
    if "author" in body:
        song.author = body["author"].strip() if body["author"] else None
    if "original_artist" in body:
        song.original_artist = body["original_artist"].strip() if body["original_artist"] else None

    await db.commit()
    return {
        "status": "ok",
        "id": song.id,
        "title": song.title,
        "author": song.author,
        "original_artist": song.original_artist,
    }


@router.post("/api/download")
async def api_download(
    req: DownloadRequest, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """API: Trigger YouTube download via Celery."""
    try:
        validate_music_url(req.url, resolve=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.openai_api_key or req.openai_base_url:
        raise HTTPException(
            status_code=400,
            detail="Configure AI credentials in owner settings instead of sending them with a task",
        )
    if req.youtube_cookies:
        if req.youtube_cookies.strip().upper() == "CLEAR":
            await revoke_browser_credentials("youtube")
            setting = await settings_service.resolve_setting(db, key="youtube_cookies")
            if setting:
                await settings_service.delete_setting(db, setting.id)
                await db.commit()
            return {"status": "cleared", "url": req.url, "use_ai": req.use_ai}
        else:
            if (
                "# Netscape HTTP Cookie File" not in req.youtube_cookies
                and ".youtube.com" not in req.youtube_cookies
            ):
                raise HTTPException(status_code=400, detail="Invalid Cookie Format. Must be Netscape format.")

            await revoke_browser_credentials("youtube")
            await settings_service.upsert_setting(
                db,
                key="youtube_cookies",
                value=req.youtube_cookies,
                scope="global",
                value_type="string",
                is_secret=True,
            )
    await dispatch_tracked_async(
        process_youtube_url_task,
        redis_client,
        "music_dl",
        {"url": req.url, "title": "Resolving URL...", "status": "Queued", "progress": "0%"},
        args=(req.url, req.use_ai, None, None, req.playlist_id),
    )
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
    package_id = request.query_params.get("package_id")
    package_scope, _ = _music_package_scope(package_id)
    return templates.TemplateResponse(
        request,
        "music.html",
        {
            "user": user,
            "lang": lang,
            "package_id": package_id,
            "package_mode": bool(package_id),
            "package_scope": package_scope,
        },
    )


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
    package_id = request.query_params.get("package_id")
    package_scope, _ = _music_package_scope(package_id)
    songs = await api_list_songs(search=search, db=db, user=user, package_id=package_id)
    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "songs": songs,
            "lang": lang,
            "search": search or "",
            "package_mode": bool(package_id),
            "package_scope": package_scope,
        },
    )


@router.delete("/ui/songs/{song_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_song_ui(song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    song = await db.get(Song, song_id)
    if song:
        from app.modules.music.models import PlaylistSong

        playlist_ids = (
            (await db.execute(select(PlaylistSong.playlist_id).where(PlaylistSong.song_id == song_id)))
            .scalars()
            .all()
        )
        await db.delete(song)
        await db.flush()
        for playlist_id in playlist_ids:
            playlist = await db.get(Playlist, playlist_id)
            if playlist:
                await _regenerate_playlist_cover(db, playlist)
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
        playlist = await db.get(Playlist, playlist_id)
        if playlist:
            await db.flush()
            await _regenerate_playlist_cover(db, playlist)
        await db.commit()
    return ""


@router.delete("/ui/playlists/{playlist_id}", response_class=HTMLResponse, include_in_schema=False)
async def delete_playlist_ui(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    playlist = await db.get(Playlist, playlist_id)
    if playlist:
        from app.core.storage import get_storage

        if playlist.cover_path:
            await anyio.to_thread.run_sync(get_storage().delete_file, playlist.cover_path)
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
    package_id = request.query_params.get("package_id")
    _music_package_scope(package_id)
    playlists = await api_list_playlists(db, user, package_id)
    return templates.TemplateResponse(
        request,
        "playlists.html",
        {"playlists": playlists, "lang": lang, "package_mode": bool(package_id)},
    )


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
    package_id = request.query_params.get("package_id")
    songs = await api_list_playlist_songs(playlist_id, db, user, package_id)
    return templates.TemplateResponse(
        request,
        "playlist_detail.html",
        {"playlist": playlist, "songs": songs, "lang": lang, "package_mode": bool(package_id)},
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

    escaped_search = escape(search or "")
    escaped_placeholder = escape(_t("search_library_placeholder", lang))
    html = f"""
    <div class="mb-4">
        <input type="text" name="search" value="{escaped_search}"
               placeholder="{escaped_placeholder}"
               hx-get="/music/ui/playlists/{playlist_id}/library"
               hx-trigger="keyup changed delay:200ms"
               hx-target="#playlist-library-sidebar"
               class="w-full bg-black border border-zinc-800 rounded-none px-3 py-1.5 text-xs font-mono text-zinc-100 placeholder-zinc-700 focus:border-teal-400 focus:outline-none transition-colors">
    </div>
    <div class="space-y-2 overflow-y-auto max-h-[400px] pr-2">
    """
    if not songs:
        html += f'<div class="text-xs font-mono text-zinc-600 text-center py-4">{escape(_t("no_songs_in_library", lang))}</div>'
    for song in songs:
        is_added = song.id in in_playlist_ids
        btn = (
            f'<span class="text-[10px] font-mono text-zinc-500 bg-zinc-900 border border-zinc-800 px-2 py-0.5">{escape(_t("in_list", lang))}</span>'
            if is_added
            else f"""
        <button hx-post="/music/ui/playlists/{playlist_id}/songs/{song.id}"
                hx-target="this"
                hx-swap="outerHTML"
                class="text-[10px] font-mono text-emerald-400 border border-emerald-500/30 bg-emerald-950/20 px-2 py-0.5 hover:bg-emerald-500 hover:text-black transition-all">
            {escape(_t("add", lang))}
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

        escaped_title = escape(song.title)
        escaped_author = escape(song.author or "Unknown")
        html += f"""
        <div class="flex items-center gap-3 p-2 bg-zinc-950/40 border border-zinc-900/60 hover:border-zinc-800">
            {cover_html}
            <div class="flex-1 min-w-0">
                <div class="text-xs font-semibold text-zinc-200 truncate" title="{escaped_title}">{escaped_title}</div>
                <div class="text-[10px] text-zinc-500 truncate">{escaped_author}</div>
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
        return HTMLResponse(
            f'<span class="text-zinc-500 font-mono text-[10px]">{escape(_t("in_list", lang))}</span>'
        )

    pos_stmt = select(func.coalesce(func.max(PlaylistSong.position), -1)).where(
        PlaylistSong.playlist_id == playlist_id
    )
    pos_res = await db.execute(pos_stmt)
    max_pos = pos_res.scalar()
    if max_pos is None:
        max_pos = -1

    ps = PlaylistSong(playlist_id=playlist_id, song_id=song_id, position=max_pos + 1)
    db.add(ps)
    playlist = await db.get(Playlist, playlist_id)
    if playlist:
        await db.flush()
        await _regenerate_playlist_cover(db, playlist)
    await db.commit()

    response = HTMLResponse(
        f'<span class="text-emerald-400 font-bold font-mono text-[10px]">{escape(_t("added", lang))}</span>'
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
            f'<div class="text-red-500 font-mono text-xs border border-red-500 p-2">{escape(e.detail)}</div>'
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
    keys = []
    for pattern in ("music_dl:*", "music_convert:*"):
        keys.extend([key async for key in redis_client.scan_iter(match=pattern, count=100)])
    downloads = []
    for k in keys:
        data = await redis_client.get(k)
        if data:
            payload = json.loads(data)
            if not is_terminal_task_payload(payload):
                downloads.append(payload)

    if not downloads:
        return HTMLResponse('<div class="text-xs font-mono text-zinc-600">No active downloads</div>')

    html = '<div class="space-y-2">'
    for d in downloads:
        title = escape(d.get("title") or "")
        task_id = escape(d.get("task_id") or "")
        status = escape(d.get("status") or "")
        progress = escape(d.get("progress") or "")
        html += f'''
        <div class="border border-zinc-800 bg-zinc-950 p-2 flex flex-col gap-1">
            <div class="flex justify-between items-center">
                <span class="text-[10px] font-mono text-emerald-400 truncate max-w-[200px]" title="{title}">{title}</span>
                <button hx-delete="/music/ui/downloads/{task_id}" hx-swap="outerHTML" class="text-red-500 hover:text-red-400 font-bold ml-2">❌</button>
            </div>
            <div class="flex justify-between items-center text-[9px] font-mono text-zinc-500">
                <span>{status}</span>
                <span>{progress}</span>
            </div>
        </div>
        '''
    html += "</div>"
    return HTMLResponse(html)


@router.delete("/ui/downloads/all", response_class=HTMLResponse, include_in_schema=False)
async def cancel_all_downloads_ui(request: Request, user=Depends(get_current_user)):
    """HTMX endpoint to cancel all active downloads."""
    from app.core.scheduler import celery_app

    keys = []
    for pattern in ("music_dl:*", "music_convert:*"):
        keys.extend([key async for key in redis_client.scan_iter(match=pattern, count=100)])
    for k in keys:
        data = await redis_client.get(k)
        if data:
            parsed = json.loads(data)
            if is_terminal_task_payload(parsed):
                continue
            task_id = parsed.get("task_id")
            if task_id:
                celery_app.control.revoke(task_id, terminate=True)
        await redis_client.delete(k)
    return HTMLResponse('<div class="text-xs font-mono text-zinc-600">No active downloads</div>')


@router.delete("/ui/downloads/{task_id}", response_class=HTMLResponse, include_in_schema=False)
async def cancel_download_ui(task_id: str, request: Request, user=Depends(get_current_user)):
    """HTMX endpoint to cancel an active download."""
    from app.core.scheduler import celery_app

    celery_app.control.revoke(task_id, terminate=True)
    await redis_client.delete(f"music_dl:{task_id}")
    await redis_client.delete(f"music_convert:{task_id}")
    return HTMLResponse("")


@router.get("/api/conversions/{task_id}")
async def get_conversion_status(task_id: str, user=Depends(get_current_user)):
    """Return persisted progress and the resulting Music audio URL."""
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="Conversion task not found")
    raw = await redis_client.get(f"music_convert:{task_id}")
    if not raw:
        raise HTTPException(status_code=404, detail="Conversion task not found")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Conversion status is unavailable") from exc


@router.get("/api/songs/{song_id}/sync-manifest")
async def get_song_sync_manifest(
    song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user), hybrid: bool = True
):
    """API: Generates a NetOutpost sync manifest for a specific song."""
    song = await db.get(Song, song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found")

    pkg_id = f"song_{song_id}"
    resources = [
        {"url": "/static/tailwind.css", "type": "css"},
        {"url": "/static/htmx.min.js", "type": "js"},
        {"url": f"/music/dashboard?package_id={pkg_id}", "type": "html"},
        {"url": f"/music/ui/player?package_id={pkg_id}", "type": "html"},
        {"url": f"/music/api/songs?package_id={pkg_id}", "type": "json"},
        {"url": f"/music/audio/{song_id}", "type": "binary"},
    ]
    if song.cover_file_id:
        resources.append({"url": f"/music/cover/{song_id}", "type": "image"})

    title_str = f"Song: {song.title}" + (f" - {song.author}" if song.author else "")
    from app.core.packages_router import make_package_manifest

    manifest = make_package_manifest(
        module_id="music",
        package_id=pkg_id,
        package_title=title_str,
        root_url=f"/music/dashboard?package_id={pkg_id}",
        resources=resources,
    )
    if hybrid:
        from app.core.packages_router import make_hybrid_manifest

        return make_hybrid_manifest(pkg_id, manifest)
    return manifest


@router.get("/api/playlists/{playlist_id}/sync-manifest")
async def get_playlist_sync_manifest(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user), hybrid: bool = True
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

    pkg_id = f"playlist_{playlist_id}"
    resources = [
        {"url": "/static/tailwind.css", "type": "css"},
        {"url": "/static/htmx.min.js", "type": "js"},
        {"url": f"/music/dashboard?package_id={pkg_id}", "type": "html"},
        {"url": f"/music/ui/player?package_id={pkg_id}", "type": "html"},
        {"url": f"/music/ui/playlists/{playlist_id}?package_id={pkg_id}", "type": "html"},
        {"url": f"/music/ui/playlists?package_id={pkg_id}", "type": "html"},
        {"url": f"/music/api/playlists?package_id={pkg_id}", "type": "json"},
        {"url": f"/music/api/playlists/{playlist_id}/songs?package_id={pkg_id}", "type": "json"},
    ]
    if playlist.cover_path:
        resources.append({"url": f"/music/playlists/{playlist_id}/cover", "type": "image"})
    for song in songs:
        resources.append({"url": f"/music/audio/{song.id}", "type": "binary"})
        if song.cover_file_id:
            resources.append({"url": f"/music/cover/{song.id}", "type": "image"})

    playlist_title = f"Music Playlist: {playlist.name}"
    from app.core.packages_router import make_package_manifest

    manifest = make_package_manifest(
        module_id="music",
        package_id=pkg_id,
        package_title=playlist_title,
        root_url=f"/music/dashboard?package_id={pkg_id}",
        resources=resources,
    )
    if hybrid:
        from app.core.packages_router import make_hybrid_manifest

        return make_hybrid_manifest(pkg_id, manifest)
    return manifest


# ── Shared Media Endpoints ───────────────────────────────


@router.get("/audio/{song_id}")
async def get_audio(
    request: Request, song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Stream audio file from storage (supports cookie and bearer token)."""
    song = await db.get(Song, song_id)
    if not song or not song.audio_file_id:
        raise HTTPException(status_code=404, detail="Audio not found")

    from app.core.responses import serve_media_stream

    return serve_media_stream(request, song.audio_file_id)


@router.get("/cover/{song_id}")
async def get_cover(song_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Serve cover image from storage."""
    song = await db.get(Song, song_id)
    if not song or not song.cover_file_id:
        raise HTTPException(status_code=404, detail="Cover not found")

    from app.core.responses import serve_storage_file_chunked

    return serve_storage_file_chunked(song.cover_file_id)


@router.get("/playlists/{playlist_id}/cover")
async def get_playlist_cover(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Serve the generated playlist cover from storage."""
    playlist = await db.get(Playlist, playlist_id)
    if not playlist or not playlist.cover_path:
        raise HTTPException(status_code=404, detail="Playlist cover not found")

    from app.core.responses import serve_storage_file_chunked

    return serve_storage_file_chunked(playlist.cover_path)
