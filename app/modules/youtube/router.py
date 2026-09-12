import asyncio
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.browser_client import revoke_browser_credentials
from app.core.browser_snapshots import browser_snapshot_store
from app.core.database import get_db
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked, validate_remote_url
from app.core.security import get_current_user
from app.core.templates import templates
from app.modules.settings import service as settings_service
from app.modules.settings.models import Setting
from app.modules.youtube.services import (
    YouTubeAPIError,
    YouTubeClient,
    clear_cache,
    load_cookies,
    load_shorts_enabled,
    resolve_stream,
    video_url,
)

router = APIRouter()
THUMBNAIL_HOSTS = frozenset(
    {"i.ytimg.com", "yt3.ggpht.com", "yt3.googleusercontent.com", "lh3.googleusercontent.com"}
)
GOOGLEVIDEO_HOSTS = frozenset({"googlevideo.com"})
RANGE_HEADER = re.compile(r"^bytes=(?:\d+-\d*|-\d+)$")


def _api_error(exc: YouTubeAPIError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def _client(db: AsyncSession | None = None) -> YouTubeClient:
    return YouTubeClient(await load_cookies())


class YouTubeSettingsUpdate(BaseModel):
    shorts_enabled: bool


@router.get("/youtube/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def youtube_dashboard(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "youtube_dashboard.html",
        {"user": user, "lang": request.cookies.get("lang", "en")},
    )


@router.get("/youtube/watch/{video_id}", response_class=HTMLResponse, include_in_schema=False)
async def youtube_watch(request: Request, video_id: str, user=Depends(get_current_user)):
    try:
        video_url(video_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "youtube_watch.html",
        {
            "user": user,
            "lang": request.cookies.get("lang", "en"),
            "video_id": video_id,
        },
    )


@router.get("/api/youtube/popular")
async def youtube_popular(
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).popular(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/recommendations")
async def youtube_recommendations(
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).recommendations(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/subscriptions")
async def youtube_subscriptions(
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).subscriptions(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/history")
async def youtube_history(
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).history(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/watch-later")
async def youtube_watch_later(
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).watch_later(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/shorts")
async def youtube_shorts(
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not await load_shorts_enabled(db):
        raise HTTPException(status_code=404, detail="YouTube Shorts are disabled")
    try:
        return await (await _client(db)).shorts(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/search")
async def youtube_search(
    q: str = Query(min_length=1, max_length=200),
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).search(q, page_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/channels/{channel_id}/videos")
async def youtube_channel_videos(
    channel_id: str,
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).channel_videos(channel_id, page_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/playlists/{playlist_id}/items")
async def youtube_playlist_items(
    playlist_id: str,
    page_token: str | None = Query(default=None, max_length=500),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        return await (await _client(db)).playlist_items(playlist_id, page_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/thumbnail", include_in_schema=False)
async def youtube_thumbnail(
    url: str = Query(max_length=2000),
    user=Depends(get_current_user),
):
    try:
        content, content_type, _ = await asyncio.to_thread(
            fetch_bytes_checked,
            url,
            allowed_hosts=THUMBNAIL_HOSTS,
            max_redirects=2,
            max_bytes=5 * 1024 * 1024,
            allowed_content_prefixes=("image/jpeg", "image/png", "image/webp"),
        )
    except (RemoteFetchError, OSError) as exc:
        raise HTTPException(status_code=404, detail="YouTube thumbnail is unavailable") from exc
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/youtube/account")
async def youtube_account_status(
    user=Depends(get_current_user),
):
    return {"authenticated": bool(await browser_snapshot_store.cookies_for_scope("youtube"))}


@router.get("/api/youtube/settings")
async def youtube_settings(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    return {"shorts_enabled": await load_shorts_enabled(db)}


@router.put("/api/youtube/settings")
async def update_youtube_settings(
    body: YouTubeSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await settings_service.upsert_setting(
        db,
        key="youtube_shorts_enabled",
        value=str(body.shorts_enabled).lower(),
        scope="module",
        module_name="youtube",
        description="Show the YouTube Shorts catalog tab",
        value_type="boolean",
    )
    await db.commit()
    await clear_cache()
    return {"shorts_enabled": body.shorts_enabled}


@router.delete("/api/youtube/account")
async def disconnect_youtube_account(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    await revoke_browser_credentials("youtube")
    await db.execute(delete(Setting).where(Setting.key == "youtube_cookies"))
    await db.commit()
    await clear_cache()
    return {"status": "disconnected", "authenticated": False}


@router.post("/api/youtube/videos/{video_id}/stream")
async def create_youtube_stream(
    video_id: str,
    user=Depends(get_current_user),
):
    try:
        return await (await _client()).create_stream(video_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/videos/{video_id}/comments")
async def youtube_video_comments(
    video_id: str,
    user=Depends(get_current_user),
):
    try:
        return {"items": await (await _client()).comments(video_id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


async def _proxy_youtube_url(remote_url: str, headers_source: dict, request: Request):
    try:
        await asyncio.to_thread(
            validate_remote_url,
            remote_url,
            allowed_hosts=GOOGLEVIDEO_HOSTS,
        )
    except (RemoteFetchError, OSError) as exc:
        raise HTTPException(status_code=502, detail="YouTube stream URL is invalid") from exc

    allowed_headers = {"user-agent", "referer", "origin"}
    headers = {key: value for key, value in headers_source.items() if key.lower() in allowed_headers}
    if range_header := request.headers.get("range"):
        if not RANGE_HEADER.fullmatch(range_header):
            raise HTTPException(status_code=416, detail="Invalid Range header")
        headers["Range"] = range_header
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), follow_redirects=False)
    try:
        upstream = await client.send(client.build_request("GET", remote_url, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail="YouTube stream is unavailable") from exc
    if upstream.status_code == 416:
        response_headers = {
            key: value for key in ("accept-ranges", "content-range") if (value := upstream.headers.get(key))
        }
        await upstream.aclose()
        await client.aclose()
        return Response(status_code=416, headers=response_headers)
    if upstream.status_code not in {200, 206}:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail="YouTube stream is unavailable")

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {
        key: value
        for key in ("accept-ranges", "content-length", "content-range")
        if (value := upstream.headers.get(key))
    }
    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=response_headers,
    )


@router.get("/api/youtube/streams/{token}", include_in_schema=False)
async def proxy_youtube_stream(token: str, request: Request, user=Depends(get_current_user)):
    stream = await resolve_stream(token)
    if not stream:
        raise HTTPException(status_code=404, detail="YouTube stream expired")
    return await _proxy_youtube_url(stream["url"], stream.get("headers", {}), request)


@router.get("/api/youtube/streams/{token}/tracks/{track_id}", include_in_schema=False)
async def proxy_youtube_mse_track(
    token: str, track_id: str, request: Request, user=Depends(get_current_user)
):
    stream = await resolve_stream(token)
    if not stream:
        raise HTTPException(status_code=404, detail="YouTube stream expired")
    track = stream.get("mse_track_urls", {}).get(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="YouTube stream track is unavailable")
    return await _proxy_youtube_url(track["url"], track.get("headers", {}), request)
