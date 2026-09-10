import asyncio
import hashlib
import json
import re
import secrets
import time
from typing import Literal
from urllib.parse import parse_qs, quote, urlparse

import redis

from app.contracts.video_source_catalog_v1 import VideoSourceItem, VideoSourceResult
from app.core.browser_snapshots import browser_snapshot_store
from app.core.config import get_settings
from app.core.ytdlp_pipeline import YtDlpErrorKind, YtDlpPipelineError, extract_info

ENTITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,255}$")
PAGE_SIZE = 24
MAX_PAGE_OFFSET = 10000
MAX_SEARCH_OFFSET = 96
DISCOVERY_QUERY = "popular videos"
SUBSCRIPTIONS_URL = "https://www.youtube.com/feed/subscriptions"

_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 128
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()
_extract_lock = asyncio.Lock()
_stream_slots = asyncio.Semaphore(2)
_redis = redis.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
_streams: dict[str, tuple[float, dict]] = {}
_streams_lock = asyncio.Lock()


class YouTubeAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def validate_entity_id(entity_id: str) -> str:
    if not ENTITY_ID_PATTERN.fullmatch(entity_id):
        raise ValueError("Invalid YouTube entity ID")
    return entity_id


def video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={validate_entity_id(video_id)}"


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={validate_entity_id(playlist_id)}"


def channel_url(channel_id: str) -> str:
    return f"https://www.youtube.com/channel/{validate_entity_id(channel_id)}"


def _page_offset(page_token: str | None, max_offset: int = MAX_PAGE_OFFSET) -> int:
    if not page_token:
        return 0
    if not page_token.isdigit():
        raise YouTubeAPIError("Invalid catalog page token", status_code=400)
    offset = int(page_token)
    if offset < 0 or offset > max_offset:
        raise YouTubeAPIError("Invalid catalog page token", status_code=400)
    return offset


def _thumbnail_url(entry: dict) -> str | None:
    remote_url = entry.get("thumbnail")
    if not remote_url:
        thumbnails = entry.get("thumbnails") or []
        remote_url = next(
            (item.get("url") for item in reversed(thumbnails) if item.get("url")),
            None,
        )
    return f"/api/youtube/thumbnail?url={quote(remote_url, safe='')}" if remote_url else None


def _entry_url(entry: dict, video_id: str) -> str:
    webpage_url = entry.get("webpage_url")
    if isinstance(webpage_url, str) and _is_youtube_url(webpage_url):
        return webpage_url
    return video_url(video_id)


def _source_item(
    kind: Literal["video", "playlist", "channel"],
    entity_id: str,
    entry: dict,
) -> VideoSourceItem:
    validate_entity_id(entity_id)
    if kind == "playlist":
        entity_type = "youtube_playlist"
        source_url = playlist_url(entity_id)
    elif kind == "channel":
        entity_type = "youtube_channel"
        source_url = channel_url(entity_id)
    else:
        entity_type = "youtube_video"
        source_url = _entry_url(entry, entity_id)
    duration = entry.get("duration")
    view_count = entry.get("view_count")
    raw_channel_id = entry.get("channel_id") or entry.get("uploader_id")
    normalized_channel_id = (
        raw_channel_id
        if isinstance(raw_channel_id, str) and ENTITY_ID_PATTERN.fullmatch(raw_channel_id)
        else None
    )
    published_at = entry.get("upload_date")
    if not published_at and entry.get("timestamp") is not None:
        published_at = str(entry["timestamp"])
    return VideoSourceItem(
        entity_type=entity_type,
        entity_id=entity_id,
        kind=kind,
        title=entry.get("title") or "Untitled",
        description=entry.get("description") or "",
        channel_id=normalized_channel_id,
        channel_title=entry.get("channel") or entry.get("uploader"),
        published_at=published_at,
        thumbnail_url=_thumbnail_url(entry),
        source_url=source_url,
        duration=int(duration) if isinstance(duration, int | float) else None,
        view_count=int(view_count) if isinstance(view_count, int | float) else None,
    )


def _is_youtube_url(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def _playlist_id_from_url(value: str) -> str | None:
    if not _is_youtube_url(value):
        return None
    playlist_id = parse_qs(urlparse(value).query).get("list", [None])[0]
    if playlist_id:
        return validate_entity_id(playlist_id)
    return None


async def clear_cache() -> None:
    async with _cache_lock:
        _cache.clear()


async def load_cookies() -> str | None:
    return await browser_snapshot_store.cookies_for_scope("youtube")


async def _extract_cached(target: str, start: int, end: int, cookies_text: str | None) -> dict:
    cookie_scope = hashlib.sha256((cookies_text or "anonymous").encode()).hexdigest()
    cache_key = hashlib.sha256(json.dumps([target, start, end, cookie_scope]).encode()).hexdigest()
    now = time.monotonic()
    async with _cache_lock:
        cached = _cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    async with _extract_lock:
        async with _cache_lock:
            cached = _cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        extraction = asyncio.create_task(asyncio.to_thread(_extract_sync, target, start, end, cookies_text))
        cancelled = False
        try:
            payload = await asyncio.shield(extraction)
        except asyncio.CancelledError:
            cancelled = True
            payload = await extraction
        async with _cache_lock:
            if len(_cache) >= _CACHE_MAX_ENTRIES:
                oldest = min(_cache, key=lambda key: _cache[key][0])
                _cache.pop(oldest, None)
            _cache[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, payload)
        if cancelled:
            raise asyncio.CancelledError
        return payload


def _video_entries(payload: dict) -> list[dict]:
    entries = payload.get("entries")
    if entries is None:
        entries = [payload]
    return [entry for entry in entries if entry and entry.get("id")]


def _extract_sync(target: str, start: int, end: int, cookies_text: str | None = None) -> dict:
    try:
        return extract_info(
            _redis,
            target,
            options={
                "extract_flat": "in_playlist",
                "ignoreerrors": True,
                "noplaylist": False,
                "playliststart": start + 1,
                "playlistend": end,
                "skip_download": True,
            },
            download=False,
            cookies_text=cookies_text,
            platform="youtube",
            require_authentication=bool(cookies_text),
        )
    except YtDlpPipelineError as exc:
        status_code = 429 if exc.kind is YtDlpErrorKind.RATE_LIMITED else 502
        raise YouTubeAPIError(str(exc), status_code=status_code) from exc


def _stream_info_sync(video_id: str, cookies_text: str | None) -> dict:
    try:
        return extract_info(
            _redis,
            video_url(video_id),
            options={
                "format": "best[protocol=https][vcodec!=none][acodec!=none]",
                "noplaylist": True,
                "skip_download": True,
            },
            download=False,
            cookies_text=cookies_text,
            platform="youtube",
            require_authentication=bool(cookies_text),
        )
    except YtDlpPipelineError as exc:
        status_code = 429 if exc.kind is YtDlpErrorKind.RATE_LIMITED else 502
        raise YouTubeAPIError(str(exc), status_code=status_code) from exc


class YouTubeClient:
    def __init__(self, cookies_text: str | None = None):
        self.cookies_text = cookies_text

    async def _catalog(
        self,
        target: str,
        title: str,
        page_token: str | None,
        *,
        include_playlist: bool = False,
        max_offset: int | None = None,
    ) -> VideoSourceResult:
        offset = _page_offset(page_token, max_offset or MAX_PAGE_OFFSET)
        payload = await _extract_cached(target, offset, offset + PAGE_SIZE, self.cookies_text)
        entries = _video_entries(payload)
        items: list[VideoSourceItem] = []
        if include_playlist and offset == 0:
            playlist_id = _playlist_id_from_url(target) or payload.get("id")
            if playlist_id:
                items.append(_source_item("playlist", playlist_id, payload))
        items.extend(_source_item("video", str(entry["id"]), entry) for entry in entries)
        next_offset = offset + PAGE_SIZE
        next_page_token = (
            str(next_offset)
            if len(entries) >= PAGE_SIZE and (max_offset is None or next_offset <= max_offset)
            else None
        )
        return VideoSourceResult(
            title=payload.get("title") or title,
            items=items,
            next_page_token=next_page_token,
        )

    async def popular(self, page_token: str | None = None) -> VideoSourceResult:
        offset = _page_offset(page_token, MAX_SEARCH_OFFSET)
        return await self._catalog(
            f"ytsearch{offset + PAGE_SIZE}:{DISCOVERY_QUERY}",
            "Discover",
            page_token,
            max_offset=MAX_SEARCH_OFFSET,
        )

    async def subscriptions(self, page_token: str | None = None) -> VideoSourceResult:
        if not self.cookies_text:
            raise YouTubeAPIError("Connect a YouTube account to load subscriptions", status_code=401)
        return await self._catalog(SUBSCRIPTIONS_URL, "Subscriptions", page_token)

    async def search(self, query: str, page_token: str | None = None) -> VideoSourceResult:
        query = query.strip()
        if not query:
            raise YouTubeAPIError("Search query is required", status_code=400)
        if _is_youtube_url(query):
            return await self._catalog(
                query,
                "YouTube",
                page_token,
                include_playlist=bool(_playlist_id_from_url(query)),
            )
        offset = _page_offset(page_token, MAX_SEARCH_OFFSET)
        target = f"ytsearch{offset + PAGE_SIZE}:{query}"
        return await self._catalog(
            target,
            f"Search: {query}",
            page_token,
            max_offset=MAX_SEARCH_OFFSET,
        )

    async def channel_videos(self, channel_id: str, page_token: str | None = None) -> VideoSourceResult:
        return await self._catalog(
            f"{channel_url(channel_id)}/videos",
            "Channel",
            page_token,
        )

    async def playlist_items(self, playlist_id: str, page_token: str | None = None) -> VideoSourceResult:
        return await self._catalog(
            playlist_url(playlist_id),
            "Playlist",
            page_token,
        )

    async def create_stream(self, video_id: str) -> dict:
        video_id = validate_entity_id(video_id)
        async with _stream_slots:
            extraction = asyncio.create_task(
                asyncio.to_thread(_stream_info_sync, video_id, self.cookies_text)
            )
            try:
                info = await asyncio.shield(extraction)
            except asyncio.CancelledError:
                await extraction
                raise
        remote_url = info.get("url")
        if not isinstance(remote_url, str) or not remote_url.startswith("https://"):
            raise YouTubeAPIError("YouTube did not return a playable stream")
        token = secrets.token_urlsafe(24)
        stream = {
            "url": remote_url,
            "headers": info.get("http_headers") or {},
            "title": info.get("title") or "YouTube video",
            "duration": info.get("duration"),
            "thumbnail_url": _thumbnail_url(info),
        }
        async with _streams_lock:
            now = time.monotonic()
            expired = [key for key, (expires, _) in _streams.items() if expires <= now]
            for key in expired:
                _streams.pop(key, None)
            _streams[token] = (now + 6 * 3600, stream)
        return {
            "title": stream["title"],
            "duration": stream["duration"],
            "thumbnail_url": stream["thumbnail_url"],
            "stream_url": f"/api/youtube/streams/{token}",
        }


async def resolve_stream(token: str) -> dict | None:
    async with _streams_lock:
        stored = _streams.get(token)
        if not stored:
            return None
        expires, stream = stored
        if expires <= time.monotonic():
            _streams.pop(token, None)
            return None
        _streams[token] = (time.monotonic() + 6 * 3600, stream)
        return stream
