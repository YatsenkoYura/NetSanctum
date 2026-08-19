import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qs, urlparse

import yt_dlp

from app.core.config import get_settings

YOUTUBE_HOSTS = frozenset({"youtube.com", "youtu.be"})

_GATE_SCRIPT = """
local current = redis.call('TIME')
local now = current[1] * 1000 + math.floor(current[2] / 1000)
local cooldown = redis.call('PTTL', KEYS[2])
if cooldown > 0 then
    return {-1, cooldown}
end
local interval = tonumber(ARGV[1])
local last_at = tonumber(redis.call('GET', KEYS[1]) or '0')
local wait = last_at + interval - now
if wait > 0 then
    return {0, wait}
end
redis.call('PSETEX', KEYS[1], math.max(interval * 4, 1000), now)
return {1, 0}
"""


class YtDlpErrorKind(StrEnum):
    AUTH_REQUIRED = "auth_required"
    GEO_BLOCKED = "geo_blocked"
    NETWORK = "network"
    PO_TOKEN_REQUIRED = "po_token_required"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class YtDlpPipelineError(RuntimeError):
    def __init__(self, kind: YtDlpErrorKind, detail: str, *, authenticated: bool = False):
        self.kind = kind
        self.detail = detail
        self.authenticated = authenticated
        super().__init__(f"{kind.value}: {detail}")


def is_youtube_url(url: str) -> bool:
    if url.startswith(("ytsearch:", "ytsearch1:")):
        return True
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(hostname == host or hostname.endswith(f".{host}") for host in YOUTUBE_HOSTS)


def is_youtube_playlist_url(url: str) -> bool:
    if not is_youtube_url(url) or url.startswith("ytsearch"):
        return False
    parsed = urlparse(url)
    return parsed.path.rstrip("/").endswith("/playlist") or bool(parse_qs(parsed.query).get("list"))


def is_youtube_single_video_url(url: str) -> bool:
    if not is_youtube_url(url) or url.startswith("ytsearch"):
        return False
    parsed = urlparse(url)
    if parse_qs(parsed.query).get("list"):
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    path_parts = [part for part in parsed.path.split("/") if part]
    if hostname == "youtu.be" or hostname.endswith(".youtu.be"):
        return bool(path_parts and len(path_parts[0]) == 11)
    if parsed.path.rstrip("/") == "/watch":
        query = parse_qs(parsed.query)
        return bool(query.get("v")) and not query.get("list")
    return bool(len(path_parts) >= 2 and path_parts[0] in {"embed", "live", "shorts"})


def _is_bot_challenge(error: BaseException | str) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "confirm you're not a bot",
            "confirm you’re not a bot",
            "captcha challenge",
        )
    )


def classify_ytdlp_error(error: BaseException | str) -> YtDlpErrorKind:
    message = str(error).lower()
    if any(
        marker in message
        for marker in (
            "this content isn't available, try again later",
            "rate limit",
            "rate-limit",
            "too many requests",
            "http error 429",
            "http error 403",
        )
    ):
        return YtDlpErrorKind.RATE_LIMITED
    if any(
        marker in message
        for marker in (
            "po token",
            "proof of origin",
            "proof-of-origin",
            "requested format is not available",
        )
    ):
        return YtDlpErrorKind.PO_TOKEN_REQUIRED
    if any(
        marker in message
        for marker in (
            "sign in",
            "login required",
            "authentication required",
            "private video",
            "members-only",
            "members only",
            "confirm your age",
            "age-restricted",
            "age restricted",
            "cookies are no longer valid",
        )
    ):
        return YtDlpErrorKind.AUTH_REQUIRED
    if any(
        marker in message for marker in ("not available in your country", "geo restricted", "geo-restricted")
    ):
        return YtDlpErrorKind.GEO_BLOCKED
    if any(
        marker in message
        for marker in (
            "video has been removed",
            "video is unavailable",
            "video unavailable",
            "does not exist",
            "http error 404",
        )
    ):
        return YtDlpErrorKind.UNAVAILABLE
    if any(
        marker in message
        for marker in (
            "timed out",
            "timeout",
            "temporary failure",
            "connection error",
            "connection reset",
            "network is unreachable",
            "http error 500",
            "http error 502",
            "http error 503",
            "http error 504",
        )
    ):
        return YtDlpErrorKind.NETWORK
    if "unsupported url" in message or "no suitable extractor" in message:
        return YtDlpErrorKind.UNSUPPORTED
    return YtDlpErrorKind.UNKNOWN


def error_status(error: BaseException) -> str:
    if not isinstance(error, YtDlpPipelineError):
        return str(error)
    labels = {
        YtDlpErrorKind.AUTH_REQUIRED: "Authentication required",
        YtDlpErrorKind.GEO_BLOCKED: "Region blocked",
        YtDlpErrorKind.NETWORK: "Media network error",
        YtDlpErrorKind.PO_TOKEN_REQUIRED: "PO token required",
        YtDlpErrorKind.RATE_LIMITED: "YouTube rate limit active",
        YtDlpErrorKind.UNAVAILABLE: "Media unavailable",
        YtDlpErrorKind.UNSUPPORTED: "Unsupported media URL",
        YtDlpErrorKind.UNKNOWN: "yt-dlp error",
    }
    return f"{labels[error.kind]}: {error.detail}"


def _merge_options(base: dict[str, Any], custom: Mapping[str, Any] | None) -> dict[str, Any]:
    if not custom:
        return base
    merged = {**base, **custom}
    extractor_args: dict[str, Any] = {}
    for source in (base.get("extractor_args", {}), custom.get("extractor_args", {})):
        for extractor, values in source.items():
            extractor_args[extractor] = {**extractor_args.get(extractor, {}), **values}
    if extractor_args:
        merged["extractor_args"] = extractor_args
    return merged


def youtube_options(custom: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = get_settings()
    options: dict[str, Any] = {
        "cachedir": settings.YTDLP_CACHE_DIR,
        "js_runtimes": {"deno": {}},
        "quiet": True,
        "sleep_interval_requests": settings.YOUTUBE_YTDLP_REQUEST_INTERVAL_SECONDS,
        "socket_timeout": 30,
    }
    if settings.YOUTUBE_POT_PROVIDER_URL:
        options["extractor_args"] = {
            "youtubepot-bgutilhttp": {"base_url": [settings.YOUTUBE_POT_PROVIDER_URL]}
        }
    return _merge_options(options, custom)


@contextmanager
def _cookie_file(cookies_text: str | None) -> Iterator[str | None]:
    if not cookies_text or not cookies_text.strip():
        yield None
        return
    text = cookies_text.strip().replace("\r\n", "\n").replace("\r", "\n")
    if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        text = f"# Netscape HTTP Cookie File\n# Generated by NetSanctum\n{text}"
    fd, path = tempfile.mkstemp(prefix="netsanctum_youtube_", suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w") as file:
            file.write(f"{text.rstrip()}\n")
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


def _wait_for_youtube_slot(redis_client, *, authenticated: bool) -> None:
    settings = get_settings()
    interval = (
        settings.YOUTUBE_YTDLP_AUTH_INTERVAL_SECONDS
        if authenticated
        else settings.YOUTUBE_YTDLP_PUBLIC_INTERVAL_SECONDS
    )
    interval_ms = max(int(interval * 1000), 0)
    while True:
        status, wait_ms = redis_client.eval(
            _GATE_SCRIPT,
            2,
            "netsanctum:ytdlp:youtube:last-at",
            "netsanctum:ytdlp:youtube:cooldown",
            interval_ms,
        )
        if int(status) < 0:
            reason = redis_client.get("netsanctum:ytdlp:youtube:cooldown") or "YouTube request limit reached"
            raise YtDlpPipelineError(
                YtDlpErrorKind.RATE_LIMITED,
                f"{reason}. Retry after approximately {max(int(wait_ms) // 1000, 1)} seconds.",
                authenticated=authenticated,
            )
        if int(status) > 0:
            return
        time.sleep(int(wait_ms) / 1000)


def _open_rate_limit_cooldown(redis_client, detail: str) -> None:
    redis_client.setex(
        "netsanctum:ytdlp:youtube:cooldown",
        get_settings().YOUTUBE_YTDLP_BACKOFF_SECONDS,
        detail,
    )


def _invoke(
    redis_client,
    url: str,
    *,
    options: Mapping[str, Any],
    download: bool,
    cookies_text: str | None,
    youtube: bool,
) -> dict[str, Any]:
    authenticated = bool(cookies_text)
    if youtube:
        _wait_for_youtube_slot(redis_client, authenticated=authenticated)
        effective_options = youtube_options(options)
    else:
        effective_options = dict(options)

    with _cookie_file(cookies_text) as cookie_path:
        if cookie_path:
            effective_options["cookiefile"] = cookie_path
        try:
            with yt_dlp.YoutubeDL(effective_options) as ydl:
                info = ydl.extract_info(url, download=download)
        except Exception as exc:
            kind = classify_ytdlp_error(exc)
            if youtube and authenticated and _is_bot_challenge(exc):
                kind = YtDlpErrorKind.RATE_LIMITED
            if youtube and kind is YtDlpErrorKind.RATE_LIMITED:
                _open_rate_limit_cooldown(redis_client, str(exc))
            raise YtDlpPipelineError(kind, str(exc), authenticated=authenticated) from exc

    if not info:
        raise YtDlpPipelineError(
            YtDlpErrorKind.UNKNOWN,
            "yt-dlp returned no media information",
            authenticated=authenticated,
        )
    return info


def extract_info(
    redis_client,
    url: str,
    *,
    options: Mapping[str, Any] | None = None,
    download: bool = False,
    cookies_text: str | None = None,
    platform: str = "youtube",
) -> dict[str, Any]:
    operation_options = options or {}
    if platform != "youtube" and not is_youtube_url(url):
        return _invoke(
            redis_client,
            url,
            options=operation_options,
            download=download,
            cookies_text=cookies_text,
            youtube=False,
        )

    try:
        return _invoke(
            redis_client,
            url,
            options=operation_options,
            download=download,
            cookies_text=None,
            youtube=True,
        )
    except YtDlpPipelineError as exc:
        if _is_bot_challenge(exc.detail) and not cookies_text:
            _open_rate_limit_cooldown(redis_client, exc.detail)
            raise YtDlpPipelineError(
                YtDlpErrorKind.RATE_LIMITED,
                exc.detail,
                authenticated=False,
            ) from exc
        if (
            exc.kind is not YtDlpErrorKind.AUTH_REQUIRED and not _is_bot_challenge(exc.detail)
        ) or not cookies_text:
            raise

    return _invoke(
        redis_client,
        url,
        options=operation_options,
        download=download,
        cookies_text=cookies_text,
        youtube=True,
    )
