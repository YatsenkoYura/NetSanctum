import json
import mimetypes
import subprocess

import redis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.storage import LocalStorage, get_storage
from app.core.templates import templates
from app.modules.video_archiver.models import ArchivedVideo, VideoPlaylist, video_playlist_association
from app.modules.video_archiver.schemas import DownloadRequest, PlaylistCreate
from app.modules.video_archiver.tasks import (
    process_video_url_task,
    sync_all_videos_task,
    sync_video_metadata_task,
)

router = APIRouter()
settings = get_settings()
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


async def _get_lang(request: Request) -> str:
    """Resolve active language cookie or fall back to DB config/default."""
    lang = request.cookies.get("lang")
    if lang:
        return lang
    return "en"


# ── UI Pages ─────────────────────────────────────────────


@router.get("/video-archiver/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def video_dashboard(
    request: Request,
    user=Depends(get_current_user),
    lang: str = Depends(_get_lang),
):
    """Render the main Video Archiver Dashboard."""
    return templates.TemplateResponse(request, "video_dashboard.html", {"user": user, "lang": lang})


# ── API Endpoints ────────────────────────────────────────


@router.post("/api/video-archiver/download")
async def trigger_download(
    req: DownloadRequest, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Schedules a video/playlist download task."""
    task = process_video_url_task.delay(
        url=req.url,
        quality=req.quality,
        comments_enabled=req.comments_enabled,
        comments_type=req.comments_type,
        comments_limit=req.comments_limit,
        comments_replies=req.comments_replies,
        replies_limit=req.replies_limit,
        auto_update=req.auto_update,
        cookies_text=req.cookies_text,
        compress_video=req.compress_video,
        download_subtitles=req.download_subtitles,
    )

    # Store initial state in Redis
    data = {
        "task_id": task.id,
        "url": req.url,
        "title": "Resolving URL...",
        "status": "Processing",
        "progress": "0%",
    }
    redis_client.setex(f"video_dl:{task.id}", 86400, json.dumps(data))

    return {"task_id": task.id, "message": "Download task dispatched."}


@router.get("/api/video-archiver/videos")
async def list_videos(
    search: str | None = None,
    status: str | None = None,
    is_deleted: bool | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """API: Lists archived videos."""
    query = select(ArchivedVideo)
    if search:
        query = query.where(
            ArchivedVideo.title.ilike(f"%{search}%") | ArchivedVideo.channel_name.ilike(f"%{search}%")
        )
    if status:
        query = query.where(ArchivedVideo.status == status)
    if is_deleted is not None:
        query = query.where(ArchivedVideo.is_deleted_on_youtube == is_deleted)

    query = query.order_by(ArchivedVideo.archived_at.desc())
    res = await db.execute(query)
    videos = res.scalars().all()
    return videos


@router.get("/api/video-archiver/videos/{video_id}")
async def get_video(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Get video metadata."""
    video = await db.get(ArchivedVideo, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


@router.delete("/api/video-archiver/videos/{video_id}")
async def delete_video(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Deletes archived video files & DB record."""
    video = await db.get(ArchivedVideo, video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    storage = get_storage()
    if video.file_path:
        storage.delete_file(video.file_path)
    if video.thumbnail_path:
        storage.delete_file(video.thumbnail_path)

    await db.delete(video)
    await db.commit()
    return {"message": "Video successfully deleted."}


@router.post("/api/video-archiver/videos/{video_id}/sync")
async def sync_video(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Schedules a background metadata sync task."""
    task = sync_video_metadata_task.delay(video_id)
    return {"task_id": task.id, "message": "Sync task dispatched."}


@router.post("/api/video-archiver/sync-all")
async def sync_all(user=Depends(get_current_user)):
    """API: Dispatches sync for all archived videos."""
    task = sync_all_videos_task.delay()
    return {"task_id": task.id, "message": "Global sync dispatched."}


# ── Streaming ────────────────────────────────────────────


@router.get("/api/video-archiver/videos/{video_id}/stream", include_in_schema=False)
async def stream_video(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Streams the archived video file with seek capability."""
    video = await db.get(ArchivedVideo, video_id)
    if not video or not video.file_path:
        raise HTTPException(status_code=404, detail="Video not found")

    storage = get_storage()
    if not storage.file_exists(video.file_path):
        raise HTTPException(status_code=404, detail="Video file missing from storage")

    mime_type, _ = mimetypes.guess_type(video.file_path)

    if isinstance(storage, LocalStorage):
        full_path = storage._full_path(video.file_path)
        return FileResponse(full_path, media_type=mime_type or "video/mp4")

    # S3 Chunked streaming
    def iterfile():
        with storage.get_file_stream(video.file_path) as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(iterfile(), media_type=mime_type or "video/mp4")


@router.get("/api/video-archiver/videos/{video_id}/audio", include_in_schema=False)
async def stream_audio(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Pipes extracted audio (MP3) on-the-fly using FFmpeg without taking storage."""
    video = await db.get(ArchivedVideo, video_id)
    if not video or not video.file_path:
        raise HTTPException(status_code=404, detail="Video not found")

    storage = get_storage()
    if not storage.file_exists(video.file_path):
        raise HTTPException(status_code=404, detail="Video file missing from storage")

    if isinstance(storage, LocalStorage):
        abs_path = storage._full_path(video.file_path)
        cmd = ["ffmpeg", "-i", str(abs_path), "-vn", "-acodec", "libmp3lame", "-f", "mp3", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def iter_audio():
            try:
                while True:
                    chunk = proc.stdout.read(16384)
                    if not chunk:
                        break
                    yield chunk
            finally:
                proc.terminate()
                proc.wait()

        return StreamingResponse(iter_audio(), media_type="audio/mpeg")
    else:
        # S3 on-the-fly streaming: pipe input file stream into ffmpeg stdin
        cmd = ["ffmpeg", "-i", "pipe:0", "-vn", "-acodec", "libmp3lame", "-f", "mp3", "pipe:1"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def iter_audio_s3():
            try:
                # Stream file stream into process stdin in chunks
                # Starlette StreamingResponse reads in chunks as well
                with storage.get_file_stream(video.file_path) as s3_stream:
                    while chunk := s3_stream.read(65536):
                        proc.stdin.write(chunk)
                proc.stdin.close()

                while True:
                    chunk = proc.stdout.read(16384)
                    if not chunk:
                        break
                    yield chunk
            finally:
                proc.terminate()
                proc.wait()

        return StreamingResponse(iter_audio_s3(), media_type="audio/mpeg")


@router.get("/api/video-archiver/videos/{video_id}/thumbnail", include_in_schema=False)
async def get_thumbnail(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Serve local thumbnail from storage."""
    video = await db.get(ArchivedVideo, video_id)
    if not video or not video.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    storage = get_storage()
    if not storage.file_exists(video.thumbnail_path):
        raise HTTPException(status_code=404, detail="Thumbnail missing from storage")

    mt, _ = mimetypes.guess_type(video.thumbnail_path)
    with storage.get_file_stream(video.thumbnail_path) as f:
        content = f.read()
    return Response(content=content, media_type=mt or "image/jpeg")


@router.get("/api/video-archiver/videos/{video_id}/subtitles/{lang}", include_in_schema=False)
async def get_subtitle(
    video_id: str, lang: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Serve subtitle file from storage."""
    video = await db.get(ArchivedVideo, video_id)
    if not video or not video.subtitles or lang not in video.subtitles:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    subtitle_path = video.subtitles[lang]
    storage = get_storage()
    if not storage.file_exists(subtitle_path):
        raise HTTPException(status_code=404, detail="Subtitle file missing from storage")

    with storage.get_file_stream(subtitle_path) as f:
        content = f.read()
    return Response(content=content, media_type="text/vtt")


# ── Active Tasks API ─────────────────────────────────────


@router.get("/api/video-archiver/tasks/active")
async def active_downloads(user=Depends(get_current_user)):
    """Fetch active download statuses from Redis."""
    keys = redis_client.keys("video_dl:*")
    tasks = []
    for k in keys:
        try:
            val = redis_client.get(k)
            if val:
                tasks.append(json.loads(val))
        except Exception:
            pass
    return tasks


@router.delete("/api/video-archiver/tasks/all")
async def cancel_all_downloads(user=Depends(get_current_user)):
    """Cancel all active downloads and purge the Celery queue."""
    from app.core.scheduler import celery_app

    try:
        celery_app.control.purge()
    except Exception:
        pass

    keys = redis_client.keys("video_dl:*")
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
    return {"message": "All downloads cancelled and queue purged."}


@router.delete("/api/video-archiver/tasks/{task_id}")
async def cancel_single_download(task_id: str, user=Depends(get_current_user)):
    """Cancel a single active download."""
    from app.core.scheduler import celery_app

    try:
        celery_app.control.revoke(task_id, terminate=True)
    except Exception:
        pass
    # Locate key if it contains the task_id
    keys = redis_client.keys("video_dl:*")
    for k in keys:
        try:
            val = redis_client.get(k)
            if val:
                data = json.loads(val)
                if data.get("task_id") == task_id:
                    redis_client.delete(k)
        except Exception:
            pass
    # Fallback delete
    redis_client.delete(f"video_dl:{task_id}")
    return {"message": f"Task {task_id} cancelled."}


# ── Playlists API ────────────────────────────────────────


@router.post("/api/video-archiver/playlists")
async def create_playlist(
    req: PlaylistCreate, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Create a new custom video playlist."""
    playlist = VideoPlaylist(name=req.name, description=req.description)
    db.add(playlist)
    await db.commit()
    await db.refresh(playlist)
    return playlist


@router.get("/api/video-archiver/playlists")
async def list_playlists(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """List all custom video playlists."""
    res = await db.execute(select(VideoPlaylist).order_by(VideoPlaylist.created_at.desc()))
    return res.scalars().all()


@router.get("/api/video-archiver/playlists/{playlist_id}")
async def get_playlist_detail(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Get playlist details and its videos."""
    playlist = await db.get(VideoPlaylist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    # Load videos
    res = await db.execute(
        select(ArchivedVideo)
        .join(video_playlist_association)
        .where(video_playlist_association.c.playlist_id == playlist_id)
    )
    videos = res.scalars().all()
    return {
        "id": playlist.id,
        "name": playlist.name,
        "description": playlist.description,
        "created_at": playlist.created_at,
        "videos": videos,
    }


@router.delete("/api/video-archiver/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Delete a custom playlist (does not delete the actual videos)."""
    playlist = await db.get(VideoPlaylist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    await db.delete(playlist)
    await db.commit()
    return {"message": "Playlist deleted."}


@router.post("/api/video-archiver/playlists/{playlist_id}/videos/{video_id}")
async def add_video_to_playlist(
    playlist_id: int, video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Link an archived video to a custom playlist."""
    playlist = await db.get(VideoPlaylist, playlist_id)
    video = await db.get(ArchivedVideo, video_id)
    if not playlist or not video:
        raise HTTPException(status_code=404, detail="Playlist or Video not found")

    # Check if link already exists
    res = await db.execute(
        select(video_playlist_association).where(
            (video_playlist_association.c.playlist_id == playlist_id)
            & (video_playlist_association.c.video_id == video_id)
        )
    )
    if res.first():
        return {"message": "Video already linked to playlist."}

    # Append
    stmt = video_playlist_association.insert().values(video_id=video_id, playlist_id=playlist_id)
    await db.execute(stmt)
    await db.commit()
    return {"message": "Video linked to playlist."}


@router.delete("/api/video-archiver/playlists/{playlist_id}/videos/{video_id}")
async def remove_video_from_playlist(
    playlist_id: int, video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Unlink an archived video from a custom playlist."""
    stmt = delete(video_playlist_association).where(
        (video_playlist_association.c.playlist_id == playlist_id)
        & (video_playlist_association.c.video_id == video_id)
    )
    await db.execute(stmt)
    await db.commit()
    return {"message": "Video unlinked from playlist."}
