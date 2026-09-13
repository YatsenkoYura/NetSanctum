import asyncio
import hashlib
import json
import re
import secrets
import threading
import time
from typing import Literal
from urllib.parse import parse_qs, quote, quote_plus, urljoin, urlparse

import redis
import requests
from requests.cookies import RequestsCookieJar, create_cookie

from app.contracts.video_source_catalog_v1 import VideoSourceItem, VideoSourceResult
from app.core.browser_client import browser_runtime_client
from app.core.browser_snapshots import browser_snapshot_store
from app.core.config import get_settings
from app.core.modules import module_registry
from app.core.ytdlp_pipeline import (
    YtDlpErrorKind,
    YtDlpPipelineError,
    classify_ytdlp_error,
    extract_info,
)
from app.modules.settings import service as settings_service

ENTITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,255}$")
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
PAGE_SIZE = 24
MAX_PAGE_OFFSET = 10000
MAX_SEARCH_OFFSET = 96
DISCOVERY_QUERY = "popular videos"
RECOMMENDATIONS_TARGET = ":ytrec"
SUBSCRIPTIONS_TARGET = ":ytsubs"
HISTORY_TARGET = ":ythistory"
WATCH_LATER_TARGET = ":ytwatchlater"
SHORTS_TARGET = ":ytshorts"
_innertube_bootstrap: dict[str, tuple[float, str, dict, str | None]] = {}
_INNERTUBE_CONTINUATION_TTL_SECONDS = 600
_INNERTUBE_CONTINUATION_MAX_ENTRIES = 256
_innertube_continuations: dict[str, tuple[float, str, str, str]] = {}
_innertube_continuations_lock = threading.Lock()

_CACHE_TTL_SECONDS = 300
_CACHE_MAX_ENTRIES = 128
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()
_extract_lock = asyncio.Lock()
_stream_slots = asyncio.Semaphore(2)
_browser_catalog_lock = asyncio.Lock()
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
    if not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise ValueError("Invalid YouTube video ID")
    return f"https://www.youtube.com/watch?v={video_id}"


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


def _proxied_thumbnail(remote_url: str | None) -> str | None:
    return f"/api/youtube/thumbnail?url={quote(remote_url, safe='')}" if remote_url else None


def _channel_images(payload: dict) -> tuple[str | None, str | None]:
    thumbnails = payload.get("thumbnails") or []
    avatar = next(
        (item.get("url") for item in reversed(thumbnails) if "avatar" in str(item.get("id", ""))),
        None,
    )
    banner = next(
        (
            item.get("url")
            for item in reversed(thumbnails)
            if "banner" in str(item.get("id", ""))
            or (item.get("width", 0) > max(item.get("height", 1), 1) * 3)
        ),
        None,
    )
    return _proxied_thumbnail(avatar), _proxied_thumbnail(banner)


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
        webpage_url = entry.get("webpage_url") or entry.get("url")
        source_url = (
            webpage_url
            if isinstance(webpage_url, str) and _is_youtube_url(webpage_url)
            else playlist_url(entity_id)
        )
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
        channel_avatar_url=_proxied_thumbnail(entry.get("channel_thumbnail")),
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
    with _innertube_continuations_lock:
        _innertube_continuations.clear()


async def load_cookies() -> str | None:
    return await browser_snapshot_store.cookies_for_scope("youtube")


async def load_shorts_enabled(db) -> bool:
    if db is None:
        return True
    setting = await settings_service.resolve_setting(db, key="youtube_shorts_enabled", module_name="youtube")
    return bool(settings_service.cast_value(setting.value, setting.value_type)) if setting else True


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
    return [entry for entry in entries if entry and isinstance(entry.get("id"), str)]


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
        status_code = {
            YtDlpErrorKind.AUTH_REQUIRED: 401,
            YtDlpErrorKind.RATE_LIMITED: 429,
        }.get(exc.kind, 502)
        raise YouTubeAPIError(str(exc), status_code=status_code) from exc


def _stream_info_sync(video_id: str, cookies_text: str | None, data_sync_id: str | None = None) -> dict:
    try:
        info = extract_info(
            _redis,
            video_url(video_id),
            options={
                "format": "best[protocol=https][vcodec!=none][acodec!=none]",
                "noplaylist": True,
                "skip_download": True,
                "extractor_args": {"youtube": {"data_sync_id": [data_sync_id]}} if data_sync_id else {},
            },
            download=False,
            cookies_text=cookies_text,
            platform="youtube",
            require_authentication=bool(cookies_text),
        )
        remote_url = info.get("url")
        if not isinstance(remote_url, str) or not remote_url.startswith("https://"):
            raise YtDlpPipelineError(YtDlpErrorKind.UNKNOWN, "YouTube did not return a playable stream")
        headers = {
            key: value
            for key, value in (info.get("http_headers") or {}).items()
            if key.lower() in {"user-agent", "referer", "origin"}
        }
        headers["Range"] = "bytes=0-0"
        response = requests.get(remote_url, headers=headers, stream=True, timeout=30)
        try:
            if response.status_code not in {200, 206}:
                raise YtDlpPipelineError(
                    classify_ytdlp_error(f"HTTP Error {response.status_code}"),
                    f"YouTube stream preflight failed with HTTP {response.status_code}",
                )
        finally:
            response.close()
        return info
    except YtDlpPipelineError as exc:
        status_code = {
            YtDlpErrorKind.AUTH_REQUIRED: 401,
            YtDlpErrorKind.RATE_LIMITED: 429,
        }.get(exc.kind, 502)
        raise YouTubeAPIError(str(exc), status_code=status_code) from exc


def _comments_sync(video_id: str, cookies_text: str | None) -> list[dict]:
    try:
        info = extract_info(
            _redis,
            video_url(video_id),
            options={
                "getcomments": True,
                "noplaylist": True,
                "skip_download": True,
                "extractor_args": {"youtube": {"max_comments": ["50,all,10,5"]}},
            },
            download=False,
            cookies_text=cookies_text,
            platform="youtube",
            require_authentication=bool(cookies_text),
        )
    except YtDlpPipelineError as exc:
        status_code = {
            YtDlpErrorKind.AUTH_REQUIRED: 401,
            YtDlpErrorKind.RATE_LIMITED: 429,
        }.get(exc.kind, 502)
        raise YouTubeAPIError(str(exc), status_code=status_code) from exc

    comments = info.get("comments") or []
    roots: list[dict] = []
    by_id: dict[str, dict] = {}
    for raw in comments[:100]:
        comment = {
            "id": str(raw.get("id") or ""),
            "author": raw.get("author") or "YouTube user",
            "author_thumbnail": _proxied_thumbnail(raw.get("author_thumbnail")),
            "text": raw.get("text") or "",
            "like_count": int(raw.get("like_count") or 0),
            "timestamp": raw.get("timestamp"),
            "replies": [],
        }
        parent = raw.get("parent")
        if parent and parent != "root" and str(parent) in by_id:
            by_id[str(parent)]["replies"].append(comment)
        else:
            roots.append(comment)
        if comment["id"]:
            by_id[comment["id"]] = comment
    return roots


def _caption_tracks(info: dict) -> list[dict]:
    tracks = []
    for automatic, groups in (
        (False, info.get("subtitles") or {}),
        (True, info.get("automatic_captions") or {}),
    ):
        for language, entries in groups.items():
            entry = next((item for item in entries if item.get("ext") == "vtt" and item.get("url")), None)
            if entry:
                tracks.append({"language": language, "automatic": automatic, "url": entry["url"]})
    return tracks


def _mse_tracks(info: dict) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Keep playable MP4 representations server-side and expose local metadata only."""
    stored: dict[str, dict] = {}
    public = {"video_tracks": [], "audio_tracks": []}
    best_video: dict[int, dict] = {}
    best_audio: dict[str, dict] = {}
    for format in info.get("formats") or []:
        if format.get("ext") not in {"mp4", "m4a"} or not isinstance(format.get("url"), str):
            continue
        vcodec = format.get("vcodec")
        acodec = format.get("acodec")
        if vcodec and vcodec != "none" and acodec == "none":
            height = format.get("height")
            if isinstance(height, int) and (
                height not in best_video or (format.get("tbr") or 0) > (best_video[height].get("tbr") or 0)
            ):
                best_video[height] = format
        elif acodec and acodec != "none" and vcodec == "none":
            language = format.get("language") or "und"
            if language not in best_audio or (format.get("abr") or 0) > (
                best_audio[language].get("abr") or 0
            ):
                best_audio[language] = format

    for index, format in enumerate(
        sorted(best_video.values(), key=lambda item: item["height"], reverse=True)
    ):
        track_id = f"v{index}"
        stored[track_id] = {
            "url": format["url"],
            "headers": {**(info.get("http_headers") or {}), **(format.get("http_headers") or {})},
        }
        public["video_tracks"].append(
            {
                "id": track_id,
                "label": f"{format['height']}p",
                "height": format["height"],
                "language": None,
                "mime": f'video/mp4; codecs="{format["vcodec"]}"',
            }
        )
    for index, (language, format) in enumerate(sorted(best_audio.items())):
        track_id = f"a{index}"
        stored[track_id] = {
            "url": format["url"],
            "headers": {**(info.get("http_headers") or {}), **(format.get("http_headers") or {})},
        }
        public["audio_tracks"].append(
            {
                "id": track_id,
                "label": language,
                "height": None,
                "language": language,
                "mime": f'audio/mp4; codecs="{format["acodec"]}"',
            }
        )
    return stored, public


def _browser_target(target: str) -> str:
    aliases = {
        RECOMMENDATIONS_TARGET: "https://www.youtube.com/",
        SUBSCRIPTIONS_TARGET: "https://www.youtube.com/feed/subscriptions",
        HISTORY_TARGET: "https://www.youtube.com/feed/history",
        WATCH_LATER_TARGET: "https://www.youtube.com/playlist?list=WL",
        SHORTS_TARGET: "https://www.youtube.com/shorts",
    }
    if target in aliases:
        return aliases[target]
    search = re.fullmatch(r"ytsearch\d*:(.*)", target, flags=re.DOTALL)
    if search:
        return f"https://www.youtube.com/results?search_query={quote_plus(search.group(1))}"
    return target


def _compact_number(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.lower().replace("\xa0", " ").replace(",", ".")
    match = re.search(r"([\d. ]+)\s*([kmb]|тыс|млн|млрд)?", normalized)
    if not match:
        return None
    suffix = match.group(2)
    try:
        number = float(match.group(1).replace(" ", "")) if suffix else int(re.sub(r"\D", "", match.group(1)))
    except ValueError:
        return None
    multiplier = {
        "k": 1_000,
        "тыс": 1_000,
        "m": 1_000_000,
        "млн": 1_000_000,
        "b": 1_000_000_000,
        "млрд": 1_000_000_000,
    }.get(suffix, 1)
    return int(number * multiplier)


def _duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.strip().split(":")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return total


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return (
            value.get("content")
            or value.get("simpleText")
            or "".join(run.get("text", "") for run in value.get("runs", []))
        )
    return ""


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _youtube_cookie_jar(cookies_text: str | None) -> RequestsCookieJar:
    """Parse only valid Netscape cookies that requests may send to YouTube."""
    jar = RequestsCookieJar()
    if not cookies_text:
        return jar
    now = time.time()
    for line in cookies_text.splitlines():
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, include_subdomains, path, secure, expires, name, value = parts
        domain = domain.lower().lstrip(".")
        if domain not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            continue
        if not name or not path.startswith("/"):
            continue
        try:
            expires_at = int(expires)
        except ValueError:
            continue
        if expires_at and expires_at <= now:
            continue
        jar.set_cookie(
            create_cookie(
                name=name,
                value=value,
                domain=f".{domain}" if include_subdomains.upper() == "TRUE" else domain,
                path=path,
                secure=secure.upper() == "TRUE",
                expires=expires_at or None,
            )
        )
    return jar


def _youtube_cookie_value(jar: RequestsCookieJar, name: str) -> str | None:
    for cookie in jar:
        if cookie.name == name and cookie.value:
            return cookie.value
    return None


def _sapisid_authorization(
    jar: RequestsCookieJar,
    origin: str = "https://www.youtube.com",
    timestamp: int | None = None,
) -> str | None:
    timestamp = int(time.time()) if timestamp is None else timestamp
    hashes = []
    sapisid = _youtube_cookie_value(jar, "SAPISID") or _youtube_cookie_value(jar, "APISID")
    if sapisid:
        digest = hashlib.sha1(f"{timestamp} {sapisid} {origin}".encode()).hexdigest()
        hashes.append(f"SAPISIDHASH {timestamp}_{digest}")
    for cookie_name, header_name in (("SAPISID1P", "SAPISID1PHASH"), ("SAPISID3P", "SAPISID3PHASH")):
        value = _youtube_cookie_value(jar, cookie_name)
        if value:
            digest = hashlib.sha1(f"{timestamp} {value} {origin}".encode()).hexdigest()
            hashes.append(f"{header_name} {timestamp}_{digest}")
    return " ".join(hashes) or None


def _innertube_bootstrap_sync(cookies_text: str | None = None) -> tuple[str, dict, str | None]:
    cache_key = hashlib.sha256((cookies_text or "anonymous").encode()).hexdigest()
    cached = _innertube_bootstrap.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1:]
    jar = _youtube_cookie_jar(cookies_text)
    response = requests.get(
        "https://www.youtube.com/",
        headers={"User-Agent": "Mozilla/5.0", "Origin": "https://www.youtube.com"},
        cookies=jar,
        timeout=30,
    )
    response.raise_for_status()
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', response.text)
    client = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', response.text)
    if not key or not client:
        raise YouTubeAPIError("Could not initialize YouTube InnerTube", status_code=503)
    context = {"client": {"clientName": "WEB", "clientVersion": client.group(1), "hl": "en"}}
    visitor = re.search(r'"(?:VISITOR_DATA|INNERTUBE_CONTEXT_CLIENT_VISITOR_DATA)":"([^"]+)"', response.text)
    if visitor:
        context["client"]["visitorData"] = visitor.group(1)
    data_sync = re.search(r'"DATASYNC_ID":"([^"]+)"', response.text)
    data_sync_id = data_sync.group(1) if data_sync else None
    _innertube_bootstrap[cache_key] = (time.monotonic() + 900, key.group(1), context, data_sync_id)
    return key.group(1), context, data_sync_id


def _innertube_continuation_token(target: str, cookies_text: str | None, continuation: str) -> str:
    now = time.monotonic()
    cookie_scope = hashlib.sha256((cookies_text or "anonymous").encode()).hexdigest()
    with _innertube_continuations_lock:
        expired = [token for token, entry in _innertube_continuations.items() if entry[0] <= now]
        for token in expired:
            del _innertube_continuations[token]
        while len(_innertube_continuations) >= _INNERTUBE_CONTINUATION_MAX_ENTRIES:
            oldest = min(_innertube_continuations, key=lambda token: _innertube_continuations[token][0])
            del _innertube_continuations[oldest]
        token = secrets.token_urlsafe(32)
        _innertube_continuations[token] = (
            now + _INNERTUBE_CONTINUATION_TTL_SECONDS,
            target,
            cookie_scope,
            continuation,
        )
    return token


def _innertube_continuation(target: str, cookies_text: str | None, page_token: str) -> str:
    cookie_scope = hashlib.sha256((cookies_text or "anonymous").encode()).hexdigest()
    with _innertube_continuations_lock:
        entry = _innertube_continuations.get(page_token)
        if entry:
            if entry[0] <= time.monotonic():
                del _innertube_continuations[page_token]
            elif entry[1:3] == (target, cookie_scope):
                return entry[3]
    raise YouTubeAPIError("Invalid or expired catalog page token", status_code=400)


def _innertube_catalog_sync(
    target: str, title: str, cookies_text: str | None, page_token: str | None = None
) -> VideoSourceResult:
    key, context, _ = _innertube_bootstrap_sync(cookies_text)
    query: dict = {"context": context}
    endpoint = "browse"
    search = re.fullmatch(r"ytsearch\d*:(.*)", target, flags=re.DOTALL)
    if search:
        endpoint = "search"
        query["query"] = search.group(1)
    else:
        query["browseId"] = {
            RECOMMENDATIONS_TARGET: "FEwhat_to_watch",
            SUBSCRIPTIONS_TARGET: "FEsubscriptions",
            HISTORY_TARGET: "FEhistory",
            WATCH_LATER_TARGET: "VLWL",
        }.get(target, "FEwhat_to_watch")
    if page_token:
        query = {
            "context": context,
            "continuation": _innertube_continuation(target, cookies_text, page_token),
        }
    origin = "https://www.youtube.com"
    headers = {
        "Origin": origin,
        "X-Origin": origin,
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": context["client"]["clientVersion"],
        "User-Agent": "Mozilla/5.0",
    }
    visitor = context["client"].get("visitorData")
    if visitor:
        headers["X-Goog-Visitor-Id"] = visitor
    jar = _youtube_cookie_jar(cookies_text)
    authorization = _sapisid_authorization(jar, origin)
    if authorization:
        headers["Authorization"] = authorization
    response = requests.post(
        f"https://www.youtube.com/youtubei/v1/{endpoint}?key={key}&prettyPrint=false",
        json=query,
        headers=headers,
        cookies=jar,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    items = []
    continuation = None
    for node in _walk(payload):
        continuation_command = (
            node.get("continuationItemRenderer", {})
            .get("continuationEndpoint", {})
            .get("continuationCommand", {})
        )
        if isinstance(continuation_command, dict) and isinstance(continuation_command.get("token"), str):
            continuation = continuation_command["token"]
        renderer = node.get("videoRenderer")
        if isinstance(renderer, dict):
            video_id = renderer.get("videoId")
            if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.fullmatch(video_id):
                continue
            thumbnail = next(
                (
                    item.get("url")
                    for item in renderer.get("thumbnail", {}).get("thumbnails", [])
                    if item.get("url")
                ),
                None,
            )
            avatar = next(
                (
                    item.get("url")
                    for item in renderer.get("channelThumbnailSupportedRenderers", {})
                    .get("channelThumbnailWithLinkRenderer", {})
                    .get("thumbnail", {})
                    .get("thumbnails", [])
                    if item.get("url")
                ),
                None,
            )
            items.append(
                VideoSourceItem(
                    entity_type="youtube_video",
                    entity_id=video_id,
                    kind="video",
                    title=_text(renderer.get("title")) or "Untitled",
                    channel_id=renderer.get("channelId"),
                    channel_title=_text(renderer.get("ownerText")),
                    channel_avatar_url=_proxied_thumbnail(avatar),
                    thumbnail_url=_proxied_thumbnail(thumbnail),
                    source_url=video_url(video_id),
                    duration=_duration_seconds(_text(renderer.get("lengthText"))),
                    view_count=_compact_number(_text(renderer.get("viewCountText"))),
                )
            )
            continue
        lockup = node.get("lockupViewModel")
        if not isinstance(lockup, dict) or lockup.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
            continue
        video_id = lockup.get("contentId")
        if not isinstance(video_id, str) or not VIDEO_ID_PATTERN.fullmatch(video_id):
            continue
        metadata = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
        rows = metadata.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
        parts = rows[0].get("metadataParts", []) if rows else []
        channel = _text(parts[0].get("text")) if parts else ""
        view_parts = rows[1].get("metadataParts", []) if len(rows) > 1 else []
        view_count = _compact_number(_text(view_parts[0].get("text"))) if view_parts else None
        thumbnail = next(
            (
                item.get("url")
                for item in lockup.get("contentImage", {})
                .get("thumbnailViewModel", {})
                .get("image", {})
                .get("sources", [])
                if item.get("url")
            ),
            None,
        )
        avatar = next(
            (
                item.get("url")
                for item in metadata.get("image", {})
                .get("decoratedAvatarViewModel", {})
                .get("avatar", {})
                .get("avatarViewModel", {})
                .get("image", {})
                .get("sources", [])
                if item.get("url")
            ),
            None,
        )
        items.append(
            VideoSourceItem(
                entity_type="youtube_video",
                entity_id=video_id,
                kind="video",
                title=_text(metadata.get("title")) or "Untitled",
                channel_title=channel or None,
                channel_avatar_url=_proxied_thumbnail(avatar),
                thumbnail_url=_proxied_thumbnail(thumbnail),
                source_url=video_url(video_id),
                view_count=view_count,
            )
        )
    return VideoSourceResult(
        title=title,
        items=items[:PAGE_SIZE],
        next_page_token=(
            _innertube_continuation_token(target, cookies_text, continuation) if continuation else None
        ),
    )


class YouTubeClient:
    def __init__(
        self,
        cookies_text: str | None = None,
        catalog_mode: Literal["innertube"] = "innertube",
    ):
        self.cookies_text = cookies_text
        self.catalog_mode = "innertube"

    async def _browser_catalog(
        self,
        target: str,
        title: str,
        page_token: str | None,
        *,
        max_offset: int | None = None,
    ) -> VideoSourceResult:
        offset = _page_offset(page_token, max_offset or MAX_PAGE_OFFSET)
        resolved = module_registry.browser_policy("youtube.account")
        if not resolved:
            raise YouTubeAPIError("YouTube browser policy is unavailable", status_code=503)
        record, policy = resolved
        await _browser_catalog_lock.acquire()
        session_id = None
        try:
            session = await browser_runtime_client.start(record.id, policy, mode="headless")
            session_id = session["session_id"]
            target_url = _browser_target(target)
            await browser_runtime_client.navigate(session_id, target_url)
            is_shorts = target == SHORTS_TARGET
            fields = {
                "title": {
                    "selector": "a#video-title, a.ytLockupMetadataViewModelTitle",
                    "attribute": "title",
                },
                "title_text": {
                    "selector": "a#video-title, a.ytLockupMetadataViewModelTitle",
                    "attribute": None,
                },
                "href": {
                    "selector": "a#video-title, a.ytLockupMetadataViewModelTitle",
                    "attribute": "href",
                },
                "thumbnail": {
                    "selector": "ytd-thumbnail img, yt-thumbnail-view-model img",
                    "attribute": "src",
                },
                "thumbnail_lazy": {
                    "selector": "ytd-thumbnail img, yt-thumbnail-view-model img",
                    "attribute": "data-thumb",
                },
                "channel": {
                    "selector": (
                        "ytd-channel-name a, #channel-name a, .ytContentMetadataViewModelMetadataRow a"
                    ),
                    "attribute": None,
                },
                "channel_href": {
                    "selector": (
                        "ytd-channel-name a, #channel-name a, .ytContentMetadataViewModelMetadataRow a"
                    ),
                    "attribute": "href",
                },
                "channel_avatar": {
                    "selector": "#avatar-link img, ytd-channel-name img, yt-avatar-shape img",
                    "attribute": "src",
                },
                "duration": {
                    "selector": ("ytd-thumbnail-overlay-time-status-renderer, yt-thumbnail-badge-view-model"),
                    "attribute": None,
                },
                "views": {
                    "selector": (
                        "#metadata-line span, .ytContentMetadataViewModelMetadataRow:nth-child(2) span"
                    ),
                    "attribute": None,
                },
            }
            if is_shorts:
                fields.update(
                    {
                        "title": {"selector": "a.ytp-title-link", "attribute": "aria-label"},
                        "title_text": {"selector": "a.ytp-title-link", "attribute": None},
                        "href": {"selector": "a.ytp-title-link", "attribute": "href"},
                        "thumbnail": {"selector": "video", "attribute": "poster"},
                        "channel": {"selector": "#channel-name a", "attribute": None},
                        "channel_href": {"selector": "#channel-name a", "attribute": "href"},
                    }
                )
            records: list[dict] = []
            previous_count = -1
            for _ in range(max(3, (offset + PAGE_SIZE) // 8)):
                await asyncio.sleep(1)
                records = await browser_runtime_client.query(
                    session_id,
                    "ytd-reel-video-renderer"
                    if is_shorts
                    else "ytd-rich-item-renderer, ytd-video-renderer, ytd-grid-video-renderer",
                    limit=min(offset + PAGE_SIZE + 1, 100),
                    fields=fields,
                )
                records = [item for item in records if item.get("href")]
                if len(records) >= offset + PAGE_SIZE or len(records) == previous_count:
                    break
                previous_count = len(records)
                await browser_runtime_client.scroll(session_id, 1800)

            items = []
            for item in records[offset : offset + PAGE_SIZE]:
                source_url = urljoin("https://www.youtube.com", item["href"])
                parsed = urlparse(source_url)
                video_id = parse_qs(parsed.query).get("v", [None])[0] or (
                    parsed.path.split("/")[-1] if "/shorts/" in parsed.path else None
                )
                if not video_id or not VIDEO_ID_PATTERN.fullmatch(video_id):
                    continue
                channel_url_value = urljoin("https://www.youtube.com", item.get("channel_href") or "")
                channel_path = urlparse(channel_url_value).path.split("/")
                channel_id = (
                    channel_path[2]
                    if len(channel_path) > 2
                    and channel_path[1] == "channel"
                    and ENTITY_ID_PATTERN.fullmatch(channel_path[2])
                    else None
                )
                items.append(
                    VideoSourceItem(
                        entity_type="youtube_video",
                        entity_id=video_id,
                        kind="video",
                        title=item.get("title") or item.get("title_text") or "Untitled",
                        channel_id=channel_id,
                        channel_title=item.get("channel"),
                        channel_avatar_url=_proxied_thumbnail(item.get("channel_avatar")),
                        thumbnail_url=_proxied_thumbnail(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"),
                        source_url=source_url,
                        duration=_duration_seconds(item.get("duration")),
                        view_count=_compact_number(item.get("views")),
                    )
                )
            next_offset = offset + PAGE_SIZE
            header = {}
            if "/channel/" in target_url:
                headers = await browser_runtime_client.query(
                    session_id,
                    "ytd-c4-tabbed-header-renderer, yt-page-header-renderer",
                    limit=1,
                    fields={
                        "title": {"selector": "#channel-name, h1", "attribute": None},
                        "description": {"selector": "#description", "attribute": None},
                        "avatar": {"selector": "#avatar img, yt-img-shadow img", "attribute": "src"},
                        "banner": {"selector": "#background img, #banner img", "attribute": "src"},
                    },
                )
                if headers:
                    header = headers[0]
            return VideoSourceResult(
                title=header.get("title") or title,
                items=items,
                next_page_token=str(next_offset) if len(records) > next_offset else None,
                description=header.get("description") or "",
                thumbnail_url=_proxied_thumbnail(header.get("banner")),
                avatar_url=_proxied_thumbnail(header.get("avatar")),
                source_url=target_url,
            )
        except (LookupError, RuntimeError, ValueError) as exc:
            raise YouTubeAPIError(str(exc), status_code=503) from exc
        finally:
            if session_id:
                try:
                    await browser_runtime_client.close(session_id)
                except Exception:
                    pass
            _browser_catalog_lock.release()

    async def _catalog(
        self,
        target: str,
        title: str,
        page_token: str | None,
        *,
        include_playlist: bool = False,
        max_offset: int | None = None,
    ) -> VideoSourceResult:
        innertube_target = target.startswith("ytsearch") or target in {
            RECOMMENDATIONS_TARGET,
            SUBSCRIPTIONS_TARGET,
            HISTORY_TARGET,
            WATCH_LATER_TARGET,
        }
        if innertube_target:
            return await asyncio.to_thread(
                _innertube_catalog_sync, target, title, self.cookies_text, page_token
            )
        if target == SHORTS_TARGET or "list=RD" in target:
            return await self._browser_catalog(
                target,
                title,
                page_token,
                max_offset=max_offset,
            )
        offset = _page_offset(page_token, max_offset or MAX_PAGE_OFFSET)
        payload = await _extract_cached(target, offset, offset + PAGE_SIZE, self.cookies_text)
        entries = _video_entries(payload)
        items: list[VideoSourceItem] = []
        if include_playlist and offset == 0:
            playlist_id = _playlist_id_from_url(target) or payload.get("id")
            if playlist_id:
                items.append(_source_item("playlist", playlist_id, payload))
        for entry in entries:
            entity_id = str(entry["id"])
            if VIDEO_ID_PATTERN.fullmatch(entity_id):
                items.append(_source_item("video", entity_id, entry))
            elif entity_id.startswith("RD") and ENTITY_ID_PATTERN.fullmatch(entity_id):
                items.append(_source_item("playlist", entity_id, entry))
        next_offset = offset + PAGE_SIZE
        next_page_token = (
            str(next_offset)
            if len(entries) >= PAGE_SIZE and (max_offset is None or next_offset <= max_offset)
            else None
        )
        avatar_url, banner_url = _channel_images(payload)
        is_channel = "/channel/" in target
        return VideoSourceResult(
            title=(payload.get("channel") if is_channel else payload.get("title")) or title,
            items=items,
            next_page_token=next_page_token,
            description=payload.get("description") or "",
            thumbnail_url=banner_url,
            avatar_url=avatar_url,
            source_url=target if _is_youtube_url(target) else None,
        )

    async def _account_catalog(
        self,
        target: str,
        title: str,
        page_token: str | None,
    ) -> VideoSourceResult:
        if not self.cookies_text:
            raise YouTubeAPIError(f"Connect a YouTube account to load {title.lower()}", status_code=401)
        try:
            return await self._catalog(target, title, page_token)
        except YouTubeAPIError as exc:
            if "yt-dlp returned no media information" in str(exc):
                return VideoSourceResult(title=title, items=[])
            raise

    async def popular(self, page_token: str | None = None) -> VideoSourceResult:
        return await self._catalog(
            f"ytsearch{PAGE_SIZE}:{DISCOVERY_QUERY}",
            "Discover",
            page_token,
            max_offset=MAX_SEARCH_OFFSET,
        )

    async def recommendations(self, page_token: str | None = None) -> VideoSourceResult:
        return await self._account_catalog(RECOMMENDATIONS_TARGET, "Recommendations", page_token)

    async def subscriptions(self, page_token: str | None = None) -> VideoSourceResult:
        return await self._account_catalog(SUBSCRIPTIONS_TARGET, "Subscriptions", page_token)

    async def history(self, page_token: str | None = None) -> VideoSourceResult:
        return await self._account_catalog(HISTORY_TARGET, "History", page_token)

    async def watch_later(self, page_token: str | None = None) -> VideoSourceResult:
        return await self._account_catalog(WATCH_LATER_TARGET, "Watch later", page_token)

    async def shorts(self, page_token: str | None = None) -> VideoSourceResult:
        return await self._account_catalog(SHORTS_TARGET, "Shorts", page_token)

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
        target = f"ytsearch{PAGE_SIZE}:{query}"
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
        if playlist_id.startswith("RD"):
            return await self._catalog(
                f"https://www.youtube.com/watch?list={playlist_id}",
                "Mix",
                page_token,
            )
        try:
            return await self._catalog(playlist_url(playlist_id), "Playlist", page_token)
        except YouTubeAPIError as exc:
            # Radio mixes are ephemeral: YouTube may expose them in a feed but reject
            # their synthetic RD playlist ID to the extractor.
            if playlist_id.startswith("RD") and "yt-dlp returned no media information" in str(exc):
                return VideoSourceResult(title="Mix", items=[])
            raise

    async def create_stream(self, video_id: str) -> dict:
        video_url(video_id)
        _, _, data_sync_id = await asyncio.to_thread(_innertube_bootstrap_sync, self.cookies_text)
        async with _stream_slots:
            extraction = asyncio.create_task(
                asyncio.to_thread(_stream_info_sync, video_id, self.cookies_text, data_sync_id)
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
        mse_track_urls, mse = _mse_tracks(info)
        stream = {
            "url": remote_url,
            "headers": info.get("http_headers") or {},
            "title": info.get("title") or "YouTube video",
            "duration": info.get("duration"),
            "thumbnail_url": _thumbnail_url(info),
            "description": info.get("description") or "",
            "channel_id": info.get("channel_id") or info.get("uploader_id"),
            "channel_title": info.get("channel") or info.get("uploader"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "published_at": info.get("upload_date") or info.get("timestamp"),
            "tags": info.get("tags") or [],
            "source_url": video_url(video_id),
            "qualities": sorted(
                {
                    int(format["height"])
                    for format in info.get("formats") or []
                    if isinstance(format.get("height"), int) and format.get("vcodec") != "none"
                },
                reverse=True,
            ),
            "audio_tracks": sorted(
                {
                    format.get("language")
                    for format in info.get("formats") or []
                    if format.get("acodec") != "none" and format.get("language")
                }
            ),
            "captions": _caption_tracks(info),
            "mse_track_urls": mse_track_urls,
            "mse": mse,
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
            "description": stream["description"],
            "channel_id": stream["channel_id"],
            "channel_title": stream["channel_title"],
            "view_count": stream["view_count"],
            "like_count": stream["like_count"],
            "comment_count": stream["comment_count"],
            "published_at": stream["published_at"],
            "tags": stream["tags"],
            "source_url": stream["source_url"],
            "qualities": stream["qualities"],
            "audio_tracks": stream["audio_tracks"],
            "captions": stream["captions"],
            "stream_url": f"/api/youtube/streams/{token}",
            "mse": {
                track_type: [
                    {**track, "url": f"/api/youtube/streams/{token}/tracks/{track['id']}"} for track in tracks
                ]
                for track_type, tracks in stream["mse"].items()
            },
        }

    async def comments(self, video_id: str) -> list[dict]:
        video_url(video_id)
        async with _stream_slots:
            return await asyncio.to_thread(_comments_sync, video_id, self.cookies_text)


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
