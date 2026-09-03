import json
import subprocess
import urllib.parse
from typing import Literal

import anyio
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.secret_values import decrypt_secret_value
from app.core.security import get_current_user
from app.core.storage import LocalStorage, get_storage
from app.core.task_dispatch import dispatch_tracked_async
from app.core.templates import templates
from app.modules.settings.models import Setting
from app.modules.video_archiver.models import ArchivedVideo, VideoChannel, VideoPlaylist
from app.modules.video_archiver.providers import PlatformRegistry
from app.modules.video_archiver.schemas import DownloadRequest, PlaylistCreate, SyncAllRequest
from app.modules.video_archiver.services import (
    ChannelService,
    PlaylistService,
    VideoService,
)
from app.modules.video_archiver.tasks import (
    process_video_url_task,
    sync_all_videos_task,
    sync_video_metadata_task,
    youtube_oauth2_task,
)
from app.modules.video_archiver.terminal_frames import (
    CC_PALETTE,
    FRAME_MEDIA_TYPES,
    extract_video_frame,
    validate_frame_dimensions,
)

router = APIRouter()
settings = get_settings()
redis_client = aioredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


async def _get_lang(request: Request) -> str:
    """Resolve active language cookie or fall back to DB config/default."""
    return request.cookies.get("lang", "en")


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
    """Schedules a video/playlist download task with automatic platform detection."""
    from app.modules.settings import service as settings_service

    if req.cookies_text and req.cookie_platform:
        key = f"{req.cookie_platform}_cookies"
        await settings_service.upsert_setting(
            db,
            key=key,
            value=req.cookies_text,
            scope="module",
            module_name="video_archiver",
            is_secret=True,
        )
        await db.commit()

    try:
        provider = PlatformRegistry.require_supported_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    detected_platform = provider.platform_id
    task = await dispatch_tracked_async(
        process_video_url_task,
        redis_client,
        "video_dl",
        {
            "url": req.url,
            "platform": detected_platform,
            "title": "Resolving URL...",
            "status": "Processing",
            "progress": "0%",
        },
        kwargs={
            "url": req.url,
            "quality": req.quality,
            "comments_enabled": req.comments_enabled,
            "comments_type": req.comments_type,
            "comments_limit": req.comments_limit,
            "comments_replies": req.comments_replies,
            "replies_limit": req.replies_limit,
            "auto_update": req.auto_update,
            "compress_video": req.compress_video,
            "download_subtitles": req.download_subtitles,
        },
    )

    return {"task_id": task.id, "platform": detected_platform, "message": "Download task dispatched."}


@router.get("/api/video-archiver/videos")
async def list_videos(
    search: str | None = None,
    channel_id: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    is_deleted: bool | None = None,
    sort_by: str = "archived_at",
    package_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """API: Lists archived videos with optional filtering by platform, channel, or search query."""
    if package_id and package_id.startswith("video_playlist_"):
        try:
            playlist_id = int(package_id.removeprefix("video_playlist_"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid package ID")
        return await PlaylistService.get_playlist_videos(db, playlist_id)
    if package_id and package_id.startswith("video_"):
        return [await VideoService.get_video(db, package_id.removeprefix("video_"))]
    return await VideoService.list_videos(
        db,
        search=search,
        channel_id=channel_id,
        platform=platform,
        status=status,
        is_deleted=is_deleted,
        sort_by=sort_by,
    )


@router.get("/api/video-archiver/videos/{video_id}")
async def get_video(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Get video metadata."""
    return await VideoService.get_video(db, video_id)


def _video_resources(video: ArchivedVideo, package_id: str) -> list[dict]:
    query = urllib.parse.urlencode({"package_id": package_id})
    resources = [
        {"url": f"/api/video-archiver/videos/{video.id}?{query}", "type": "json"},
    ]
    if video.file_path:
        resources.append({"url": f"/api/video-archiver/videos/{video.id}/stream", "type": "binary"})
    if video.thumbnail_path:
        resources.append({"url": f"/api/video-archiver/videos/{video.id}/thumbnail", "type": "image"})
    if video.channel_avatar_url:
        resources.append({"url": f"/api/video-archiver/videos/{video.id}/avatar", "type": "image"})
    if isinstance(video.subtitles, dict):
        resources.extend(
            {
                "url": (
                    f"/api/video-archiver/videos/{video.id}/subtitles/"
                    f"{urllib.parse.quote(str(language), safe='')}"
                ),
                "type": "text",
            }
            for language in video.subtitles
        )
    return resources


@router.get("/api/video-archiver/videos/{video_id}/sync-manifest")
async def get_video_sync_manifest(
    video_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    hybrid: bool = True,
):
    """Build an offline package for one archived video."""
    video = await VideoService.get_video(db, video_id)
    package_id = f"video_{video.id}"
    query = urllib.parse.urlencode({"package_id": package_id})
    list_query = urllib.parse.urlencode([("sort_by", "archived_at"), ("package_id", package_id)])
    resources = [
        {"url": "/static/tailwind.css", "type": "css"},
        {"url": "/static/htmx.min.js", "type": "js"},
        {"url": "/video-archiver/dashboard", "type": "html"},
        {"url": f"/video-archiver/dashboard?{query}", "type": "html"},
        {"url": f"/api/video-archiver/videos?{list_query}", "type": "json"},
        *_video_resources(video, package_id),
    ]
    from app.core.packages_router import make_hybrid_manifest, make_package_manifest

    manifest = make_package_manifest(
        module_id="video_archiver",
        package_id=package_id,
        package_title=f"Video: {video.title}",
        root_url=f"/video-archiver/dashboard?{query}",
        resources=resources,
    )
    return make_hybrid_manifest(package_id, manifest) if hybrid else manifest


@router.delete("/api/video-archiver/videos/{video_id}")
async def delete_video(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Deletes archived video files & DB record."""
    return await VideoService.delete_video(db, video_id)


@router.post("/api/video-archiver/videos/{video_id}/sync")
async def sync_video(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Schedules a background metadata sync task."""
    task = await dispatch_tracked_async(
        sync_video_metadata_task,
        redis_client,
        "video_dl",
        {"url": video_id, "title": video_id, "status": "Queued metadata sync", "progress": "0%"},
        args=(video_id,),
    )
    return {"task_id": task.id, "message": "Sync task dispatched."}


@router.get("/api/video-archiver/sync-dates")
async def get_sync_dates(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Returns unique update dates, platform breakdowns and cookie statuses for metadata sync UI."""
    stmt = select(ArchivedVideo.updated_at, ArchivedVideo.archived_at, ArchivedVideo.platform).where(
        ArchivedVideo.status == "completed"
    )
    res = await db.execute(stmt)

    dates_map = {}
    never_count = 0
    platform_map = {}

    for row in res.all():
        updated_at, archived_at, platform = row[0], row[1], row[2] or "youtube"
        platform_map[platform] = platform_map.get(platform, 0) + 1

        # Consider 'never' if missing or matches archived_at exactly
        if not updated_at or updated_at == archived_at:
            never_count += 1
        else:
            d_str = updated_at.strftime("%Y-%m-%d")
            dates_map[d_str] = dates_map.get(d_str, 0) + 1

    sorted_dates = [{"date": d, "count": dates_map[d]} for d in sorted(dates_map.keys(), reverse=True)]

    # Check stored cookies for each platform
    platform_info = []
    for p, count in platform_map.items():
        key = f"{p}_cookies"
        res_cookie = await db.execute(
            select(Setting).where(
                Setting.key == key,
                Setting.scope == "module",
                Setting.module_name == "video_archiver",
            )
        )
        c_setting = res_cookie.scalar_one_or_none()
        cookies_text = decrypt_secret_value(c_setting.value) if c_setting else ""

        provider = PlatformRegistry.get_provider_by_id(p)
        val_res = provider.validate_cookies(cookies_text)

        platform_info.append(
            {
                "platform": p,
                "count": count,
                "has_cookies": val_res["has_cookies"],
                "is_valid": val_res["is_valid"],
                "status": val_res["status"],
                "message": val_res["message"],
            }
        )

    return {"never": never_count, "dates": sorted_dates, "platforms": platform_info}


@router.post("/api/video-archiver/sync-all")
async def sync_all(req: SyncAllRequest, user=Depends(get_current_user)):
    """API: Dispatches sync for all archived videos."""
    task = await dispatch_tracked_async(
        sync_all_videos_task,
        redis_client,
        "video_dl",
        {"title": "Global metadata sync", "status": "Queued", "progress": "0%", "type": "global_sync"},
        kwargs={"dates": req.dates},
    )
    return {"task_id": task.id, "message": "Global sync dispatched."}


@router.post("/api/video-archiver/sync-all/cancel/{task_id}")
async def cancel_sync_all(task_id: str, user=Depends(get_current_user)):
    """API: Cancels a running global sync task."""
    from app.core.scheduler import celery_app

    celery_app.control.revoke(task_id, terminate=True)
    await redis_client.delete(f"video_dl:{task_id}")
    return {"message": "Task cancelled."}


@router.post("/api/video-archiver/youtube-oauth")
async def start_youtube_oauth(user=Depends(get_current_user)):
    """API: Starts YouTube OAuth2 flow."""
    task = await dispatch_tracked_async(
        youtube_oauth2_task,
        redis_client,
        "video_oauth",
        {"title": "YouTube OAuth", "status": "Queued", "progress": "0%"},
        ttl=3600,
    )
    return {"task_id": task.id, "message": "OAuth2 task started."}


@router.get("/api/video-archiver/cookies/{platform}")
async def get_cookies(platform: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Gets saved cookies and auth status for a platform."""
    from app.modules.settings.models import Setting

    key = f"{platform}_cookies"
    res = await db.execute(
        select(Setting).where(
            Setting.key == key,
            Setting.scope == "module",
            Setting.module_name == "video_archiver",
        )
    )
    setting = res.scalar_one_or_none()

    return {
        "platform": platform,
        "cookies_text": "",
        "has_cookies": bool(setting and setting.value),
        "auth_active": bool(setting and setting.value),
    }


@router.delete("/api/video-archiver/cookies/{platform}")
async def clear_cookies(platform: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """API: Clears saved cookies for a platform."""
    import os

    from app.modules.settings.models import Setting

    key = f"{platform}_cookies"
    res = await db.execute(
        select(Setting).where(
            Setting.key == key,
            Setting.scope == "module",
            Setting.module_name == "video_archiver",
        )
    )
    setting = res.scalar_one_or_none()
    if setting:
        await db.delete(setting)
        await db.commit()
    # If youtube, also disable OAuth
    if platform == "youtube":
        try:
            os.remove("/app/storage/.youtube_oauth_enabled")
        except FileNotFoundError:
            pass
    return {"message": f"Cookies for {platform} cleared."}


@router.get("/api/video-archiver/youtube-oauth/status/{task_id}")
async def get_youtube_oauth_status(task_id: str, user=Depends(get_current_user)):
    """API: Polls OAuth2 status and device code."""
    data = await redis_client.get(f"video_oauth:{task_id}")
    if not data:
        return {"status": "not_found"}
    return json.loads(data)


# ── Channels API ─────────────────────────────────────────


@router.get("/api/video-archiver/channels", include_in_schema=False)
async def list_channels(
    platform: str | None = None,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """API: Lists all channels with video counts and avatar paths."""
    return await ChannelService.list_channels(db, platform=platform)


@router.get("/api/video-archiver/channels/{channel_id}/avatar", include_in_schema=False)
async def get_channel_avatar_by_id(
    channel_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Serve channel avatar by channel ID."""
    ch = await db.get(VideoChannel, channel_id)
    avatar_url = ch.avatar_path if ch else None

    if not avatar_url:
        stmt = (
            select(ArchivedVideo.channel_avatar_url)
            .where((ArchivedVideo.channel_id == channel_id) & (ArchivedVideo.channel_avatar_url.isnot(None)))
            .limit(1)
        )
        res = await db.execute(stmt)
        avatar_url = res.scalar_one_or_none()

    if not avatar_url:
        raise HTTPException(status_code=404, detail="Avatar not found")

    from app.core.responses import serve_storage_file_chunked

    return serve_storage_file_chunked(avatar_url)


# ── Streaming & Subtitles ────────────────────────────────


@router.get("/api/video-archiver/videos/{video_id}/stream", include_in_schema=False)
async def stream_video(
    request: Request, video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Streams the archived video file with seek capability."""
    video = await VideoService.get_video(db, video_id)
    if not video.file_path:
        raise HTTPException(status_code=404, detail="Video file missing")

    from app.core.responses import serve_media_stream

    return serve_media_stream(request, video.file_path)


@router.get(
    "/api/video-archiver/videos/{video_id}/frame",
    responses={
        200: {
            "content": {
                "application/json": {},
                "text/plain": {},
                "image/png": {},
                "image/jpeg": {},
                "image/webp": {},
            },
            "description": "Frame encoded in the format selected by the format query parameter.",
        }
    },
)
async def get_video_frame(
    video_id: str,
    response: Response,
    time: float = Query(0, ge=0),
    width: int = Query(32, ge=1, le=2048),
    height: int = Query(18, ge=1, le=2048),
    frame_format: Literal["cc-palette", "nfp", "png", "jpeg", "webp"] = Query("cc-palette", alias="format"),
    fit: Literal["contain", "cover", "stretch"] = "contain",
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Return one frame at the requested resolution in an image or terminal format."""
    video = await VideoService.get_video(db, video_id)
    if not video.file_path:
        raise HTTPException(status_code=404, detail="Video file missing")

    storage = get_storage()
    if not await anyio.to_thread.run_sync(storage.file_exists, video.file_path):
        raise HTTPException(status_code=404, detail="Video file missing from storage")

    duration = max(0, video.duration or 0)
    timestamp = min(time, max(0, duration - 0.05)) if duration else time
    try:
        validate_frame_dimensions(width, height, frame_format)
        frame = await anyio.to_thread.run_sync(
            extract_video_frame,
            storage,
            video.file_path,
            timestamp,
            width,
            height,
            frame_format,
            fit,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Video frame extraction timed out") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    headers = {
        "Cache-Control": "no-store",
        "X-Frame-Format": frame_format,
        "X-Frame-Size": f"{width}x{height}",
        "X-Frame-Time": f"{timestamp:.3f}",
    }
    if frame_format == "cc-palette":
        response.headers.update(headers)
        return {
            "format": frame_format,
            "fit": fit,
            "width": width,
            "height": height,
            "time": round(timestamp, 3),
            "duration": duration,
            "palette": {code: f"#{red:02x}{green:02x}{blue:02x}" for code, red, green, blue in CC_PALETTE},
            "rows": frame,
        }
    if frame_format == "nfp":
        return Response(
            content="\n".join(frame) + "\n",
            media_type=FRAME_MEDIA_TYPES[frame_format],
            headers=headers,
        )
    return Response(content=frame, media_type=FRAME_MEDIA_TYPES[frame_format], headers=headers)


@router.get("/api/video-archiver/videos/{video_id}/audio", include_in_schema=False)
async def stream_audio(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Pipes extracted audio (MP3) on-the-fly using FFmpeg without taking storage."""
    video = await VideoService.get_video(db, video_id)
    if not video.file_path:
        raise HTTPException(status_code=404, detail="Video file missing")

    storage = get_storage()
    if not await anyio.to_thread.run_sync(storage.file_exists, video.file_path):
        raise HTTPException(status_code=404, detail="Video file missing from storage")

    if isinstance(storage, LocalStorage):
        abs_path = storage._full_path(video.file_path)
        cmd = ["ffmpeg", "-i", str(abs_path), "-vn", "-acodec", "libmp3lame", "-f", "mp3", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        async def iter_audio():
            try:
                while True:
                    chunk = await anyio.to_thread.run_sync(proc.stdout.read, 16384)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await anyio.to_thread.run_sync(proc.terminate)
                await anyio.to_thread.run_sync(proc.wait)

        return StreamingResponse(iter_audio(), media_type="audio/mpeg")
    else:
        import threading

        cmd = ["ffmpeg", "-i", "pipe:0", "-vn", "-acodec", "libmp3lame", "-f", "mp3", "pipe:1"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def feed_stdin():
            try:
                with storage.get_file_stream(video.file_path) as s3_stream:
                    while chunk := s3_stream.read(65536):
                        proc.stdin.write(chunk)
            except Exception:
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        async def iter_audio_s3():
            feeder = threading.Thread(target=feed_stdin, daemon=True)
            feeder.start()
            try:
                while True:
                    chunk = await anyio.to_thread.run_sync(proc.stdout.read, 16384)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await anyio.to_thread.run_sync(proc.terminate)
                await anyio.to_thread.run_sync(proc.wait)

        return StreamingResponse(iter_audio_s3(), media_type="audio/mpeg")


@router.get("/api/video-archiver/videos/{video_id}/thumbnail", include_in_schema=False)
async def get_thumbnail(video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """Serve local thumbnail from storage."""
    video = await VideoService.get_video(db, video_id)
    if not video.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    from app.core.responses import serve_storage_file_chunked

    return serve_storage_file_chunked(video.thumbnail_path)


@router.get("/api/video-archiver/videos/{video_id}/avatar", include_in_schema=False)
async def get_video_channel_avatar(
    video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Serve local channel avatar for a video from storage."""
    video = await VideoService.get_video(db, video_id)
    avatar_url = video.channel_avatar_url

    if not avatar_url and video.channel_id:
        ch = await db.get(VideoChannel, video.channel_id)
        if ch:
            avatar_url = ch.avatar_path

    if not avatar_url:
        raise HTTPException(status_code=404, detail="Avatar not found")

    from app.core.responses import serve_storage_file_chunked

    return serve_storage_file_chunked(avatar_url)


@router.get("/api/video-archiver/videos/{video_id}/subtitles/{lang}", include_in_schema=False)
async def get_subtitle(
    video_id: str, lang: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Serve subtitle file from storage."""
    video = await VideoService.get_video(db, video_id)
    if not video.subtitles or lang not in video.subtitles:
        raise HTTPException(status_code=404, detail="Subtitle not found")

    subtitle_path = video.subtitles[lang]
    from app.core.responses import serve_storage_file_chunked

    return serve_storage_file_chunked(subtitle_path)


# ── Active Tasks API ─────────────────────────────────────


@router.get("/api/video-archiver/tasks/active")
async def active_downloads(user=Depends(get_current_user)):
    """Fetch active download statuses from Redis."""
    keys = [key async for key in redis_client.scan_iter(match="video_dl:*", count=100)]
    tasks = []
    for k in keys:
        try:
            val = await redis_client.get(k)
            if val:
                tasks.append(json.loads(val))
        except Exception:
            pass
    return tasks


@router.delete("/api/video-archiver/tasks/all")
async def cancel_all_downloads(user=Depends(get_current_user)):
    """Cancel all tracked video downloads without touching other module queues."""
    from app.core.scheduler import celery_app

    keys = [key async for key in redis_client.scan_iter(match="video_dl:*", count=100)]
    for k in keys:
        try:
            val = await redis_client.get(k)
            if val:
                data = json.loads(val)
                task_id = data.get("task_id")
                if task_id:
                    celery_app.control.revoke(task_id, terminate=True)
            await redis_client.delete(k)
        except Exception:
            pass
    return {"message": "All tracked video downloads cancelled."}


@router.delete("/api/video-archiver/tasks/{task_id}")
async def cancel_single_download(task_id: str, user=Depends(get_current_user)):
    """Cancel a single active download."""
    from app.core.scheduler import celery_app

    try:
        celery_app.control.revoke(task_id, terminate=True)
    except Exception:
        pass
    keys = [key async for key in redis_client.scan_iter(match="video_dl:*", count=100)]
    for k in keys:
        try:
            val = await redis_client.get(k)
            if val:
                data = json.loads(val)
                if data.get("task_id") == task_id:
                    await redis_client.delete(k)
        except Exception:
            pass
    await redis_client.delete(f"video_dl:{task_id}")
    return {"message": f"Task {task_id} cancelled."}


# ── Playlists API ────────────────────────────────────────


@router.post("/api/video-archiver/playlists")
async def create_playlist(
    req: PlaylistCreate, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Create a new custom video playlist."""
    return await PlaylistService.create_playlist(db, req.name, req.description)


@router.get("/api/video-archiver/playlists")
async def list_playlists(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    """List all custom video playlists."""
    return await PlaylistService.list_playlists(db)


@router.get("/api/video-archiver/playlists/{playlist_id}")
async def get_playlist_detail(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Get playlist details and its videos."""
    playlist = await db.get(VideoPlaylist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")

    videos = await PlaylistService.get_playlist_videos(db, playlist_id)
    return {
        "id": playlist.id,
        "name": playlist.name,
        "description": playlist.description,
        "created_at": playlist.created_at,
        "videos": videos,
    }


@router.get("/api/video-archiver/playlists/{playlist_id}/sync-manifest")
async def get_playlist_sync_manifest(
    playlist_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    hybrid: bool = True,
):
    """Build an offline package for a custom video playlist."""
    playlist = await db.get(VideoPlaylist, playlist_id)
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found")
    videos = await PlaylistService.get_playlist_videos(db, playlist_id)
    package_id = f"video_playlist_{playlist.id}"
    query = urllib.parse.urlencode({"package_id": package_id})
    list_query = urllib.parse.urlencode([("sort_by", "archived_at"), ("package_id", package_id)])
    resources = [
        {"url": "/static/tailwind.css", "type": "css"},
        {"url": "/static/htmx.min.js", "type": "js"},
        {"url": "/video-archiver/dashboard", "type": "html"},
        {"url": f"/video-archiver/dashboard?{query}", "type": "html"},
        {"url": f"/api/video-archiver/videos?{list_query}", "type": "json"},
        {"url": f"/api/video-archiver/playlists/{playlist.id}?{query}", "type": "json"},
    ]
    for video in videos:
        resources.extend(_video_resources(video, package_id))

    from app.core.packages_router import make_hybrid_manifest, make_package_manifest

    manifest = make_package_manifest(
        module_id="video_archiver",
        package_id=package_id,
        package_title=f"Video Playlist: {playlist.name}",
        root_url=f"/video-archiver/dashboard?{query}",
        resources=resources,
    )
    return make_hybrid_manifest(package_id, manifest) if hybrid else manifest


@router.delete("/api/video-archiver/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: int, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Delete a custom playlist (does not delete the actual videos)."""
    return await PlaylistService.delete_playlist(db, playlist_id)


@router.post("/api/video-archiver/playlists/{playlist_id}/videos/{video_id}")
async def add_video_to_playlist(
    playlist_id: int, video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Link an archived video to a custom playlist."""
    return await PlaylistService.add_video(db, playlist_id, video_id)


@router.delete("/api/video-archiver/playlists/{playlist_id}/videos/{video_id}")
async def remove_video_from_playlist(
    playlist_id: int, video_id: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    """Unlink an archived video from a custom playlist."""
    return await PlaylistService.remove_video(db, playlist_id, video_id)
