import asyncio

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.browser_client import revoke_browser_credentials
from app.core.browser_snapshots import browser_snapshot_store
from app.core.database import get_db
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked, validate_remote_url
from app.core.security import get_current_user
from app.core.templates import templates
from app.modules.settings.models import Setting
from app.modules.youtube.services import (
    YouTubeAPIError,
    YouTubeClient,
    clear_cache,
    load_cookies,
    resolve_stream,
)

router = APIRouter()
THUMBNAIL_HOSTS = frozenset({"i.ytimg.com", "yt3.ggpht.com", "lh3.googleusercontent.com"})


def _api_error(exc: YouTubeAPIError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


async def _client() -> YouTubeClient:
    return YouTubeClient(await load_cookies())


@router.get("/youtube/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def youtube_dashboard(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "youtube_dashboard.html",
        {"user": user, "lang": request.cookies.get("lang", "en")},
    )


@router.get("/api/youtube/popular")
async def youtube_popular(
    page_token: str | None = Query(default=None, max_length=500),
    user=Depends(get_current_user),
):
    try:
        return await (await _client()).popular(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/subscriptions")
async def youtube_subscriptions(
    page_token: str | None = Query(default=None, max_length=500),
    user=Depends(get_current_user),
):
    try:
        return await (await _client()).subscriptions(page_token)
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/search")
async def youtube_search(
    q: str = Query(min_length=1, max_length=200),
    page_token: str | None = Query(default=None, max_length=500),
    user=Depends(get_current_user),
):
    try:
        return await (await _client()).search(q, page_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/channels/{channel_id}/videos")
async def youtube_channel_videos(
    channel_id: str,
    page_token: str | None = Query(default=None, max_length=500),
    user=Depends(get_current_user),
):
    try:
        return await (await _client()).channel_videos(channel_id, page_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except YouTubeAPIError as exc:
        raise _api_error(exc) from exc


@router.get("/api/youtube/playlists/{playlist_id}/items")
async def youtube_playlist_items(
    playlist_id: str,
    page_token: str | None = Query(default=None, max_length=500),
    user=Depends(get_current_user),
):
    try:
        return await (await _client()).playlist_items(playlist_id, page_token)
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


@router.get("/api/youtube/streams/{token}", include_in_schema=False)
async def proxy_youtube_stream(token: str, request: Request, user=Depends(get_current_user)):
    stream = await resolve_stream(token)
    if not stream:
        raise HTTPException(status_code=404, detail="YouTube stream expired")
    try:
        await asyncio.to_thread(
            validate_remote_url,
            stream["url"],
            allowed_hosts=frozenset({"googlevideo.com"}),
        )
    except (RemoteFetchError, OSError) as exc:
        raise HTTPException(status_code=502, detail="YouTube stream URL is invalid") from exc

    allowed_headers = {"user-agent", "referer", "origin"}
    headers = {
        key: value for key, value in stream.get("headers", {}).items() if key.lower() in allowed_headers
    }
    if range_header := request.headers.get("range"):
        headers["Range"] = range_header
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None), follow_redirects=False)
    try:
        upstream = await client.send(client.build_request("GET", stream["url"], headers=headers), stream=True)
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
