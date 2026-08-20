"""MangaDex adapter for the shared AllLib download pipeline."""

import html
import logging
import time
from collections import deque
from typing import Any
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

MANGADEX_SITE_ID = 8
MANGADEX_API_BASE = "https://api.mangadex.org"
MANGADEX_HOSTS = {"mangadex.org"}
MANGADEX_LANGUAGE_NAMES = {
    "ar": "Arabic",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "es-la": "Spanish (Latin America)",
    "fr": "French",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ja-ro": "Japanese (Romanized)",
    "ko": "Korean",
    "pl": "Polish",
    "pt": "Portuguese",
    "pt-br": "Portuguese (Brazil)",
    "ro": "Romanian",
    "ru": "Russian",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese (Simplified)",
    "zh-hk": "Chinese (Traditional)",
}


def is_mangadex_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in MANGADEX_HOSTS)


def _localized_value(
    values: Any, preferred_language: str = "en", original_language: str | None = None
) -> str:
    if not isinstance(values, dict):
        return ""
    for language in (preferred_language, original_language, "en"):
        if language and values.get(language):
            return str(values[language])
    return str(next(iter(values.values()), ""))


class MangaDexAPI:
    """Map MangaDex's public API to the interface consumed by AllLib tasks."""

    direct_image_urls = True

    def __init__(self, auth_token: str | None = None, language: str = "en"):
        self.session = requests.Session()
        self._auth_token = None
        self.language = language
        self.request_timestamps: deque[float] = deque()
        self.at_home_timestamps: deque[float] = deque()
        self._chapter_ids: dict[tuple[str, str, str], str] = {}

    def get_site_info_from_url(self, url: str) -> tuple[int, str]:
        return MANGADEX_SITE_ID, "mangadex.org"

    def extract_slug_from_url(self, url: str) -> str | None:
        parts = urlparse(url).path.strip("/").split("/")
        for index, part in enumerate(parts):
            if part == "title" and index + 1 < len(parts):
                manga_id = parts[index + 1]
                if len(manga_id) == 36:
                    return manga_id
        return None

    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        while self.request_timestamps and self.request_timestamps[0] <= now - 1:
            self.request_timestamps.popleft()
        if len(self.request_timestamps) >= 5:
            time.sleep(max(0, self.request_timestamps[0] + 1 - now))
        self.request_timestamps.append(time.monotonic())

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{MANGADEX_API_BASE}/{path.lstrip('/')}"
        delay = 1.0
        for attempt in range(3):
            if path.lstrip("/").startswith("at-home/"):
                self._wait_for_at_home_limit()
            self._wait_for_rate_limit()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "NetSanctum/0.1 (self-hosted MangaDex client)",
                    },
                    timeout=20,
                )
                if response.status_code == 200:
                    data = response.json()
                    if not isinstance(data, dict) or data.get("result") == "error":
                        raise RuntimeError(f"MangaDex returned an invalid response for {url}")
                    return data
                if response.status_code == 429:
                    retry_at = response.headers.get("X-RateLimit-Retry-After")
                    if retry_at:
                        delay = max(delay, float(retry_at) - time.time())
                elif response.status_code not in {502, 503, 504}:
                    response.raise_for_status()
            except (requests.RequestException, ValueError):
                if attempt == 2:
                    raise
            if attempt < 2:
                time.sleep(max(0.1, delay))
                delay *= 2
        raise RuntimeError(f"MangaDex request failed: {url}")

    def _wait_for_at_home_limit(self) -> None:
        now = time.monotonic()
        while self.at_home_timestamps and self.at_home_timestamps[0] <= now - 60:
            self.at_home_timestamps.popleft()
        if len(self.at_home_timestamps) >= 40:
            time.sleep(max(0, self.at_home_timestamps[0] + 60 - now))
            now = time.monotonic()
            while self.at_home_timestamps and self.at_home_timestamps[0] <= now - 60:
                self.at_home_timestamps.popleft()
        self.at_home_timestamps.append(time.monotonic())

    def get_novel_info(
        self, slug: str, site_id: int = MANGADEX_SITE_ID, domain: str = "mangadex.org"
    ) -> dict[str, Any]:
        data = self._request_json(
            f"/manga/{slug}",
            {"includes[]": ["cover_art", "author", "artist"]},
        ).get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"MangaDex title {slug!r} was not found")

        attributes = data.get("attributes") or {}
        original_language = attributes.get("originalLanguage")
        title_values = attributes.get("title") or {}
        title = _localized_value(title_values, self.language, original_language) or slug
        description = _localized_value(attributes.get("description"), self.language, original_language)
        relationships = data.get("relationships") or []

        authors: list[dict[str, str]] = []
        artists: list[dict[str, str]] = []
        cover_url = None
        for relationship in relationships:
            if not isinstance(relationship, dict):
                continue
            rel_type = relationship.get("type")
            rel_attributes = relationship.get("attributes") or {}
            if rel_type == "cover_art" and rel_attributes.get("fileName"):
                cover_url = f"https://uploads.mangadex.org/covers/{slug}/{rel_attributes['fileName']}.512.jpg"
            elif rel_type in {"author", "artist"} and rel_attributes.get("name"):
                target = authors if rel_type == "author" else artists
                target.append({"name": str(rel_attributes["name"])})

        tags = []
        for tag in attributes.get("tags") or []:
            name = _localized_value((tag.get("attributes") or {}).get("name"), self.language)
            if name:
                tags.append({"name": name})

        return {
            "id": slug,
            "name": title,
            "rus_name": title_values.get("ru"),
            "eng_name": title_values.get("en"),
            "summary": html.escape(description),
            "year": attributes.get("year"),
            "status": attributes.get("status"),
            "format": attributes.get("publicationDemographic"),
            "authors": authors,
            "artists": artists,
            "tags": tags,
            "cover": {"default": cover_url} if cover_url else None,
            "available_languages": [
                str(language) for language in attributes.get("availableTranslatedLanguages") or [] if language
            ],
        }

    def get_novel_chapters(
        self, slug: str, site_id: int = MANGADEX_SITE_ID, domain: str = "mangadex.org"
    ) -> list[dict[str, Any]]:
        chapters: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        offset = 0

        while True:
            response = self._request_json(
                f"/manga/{slug}/feed",
                {
                    "limit": 500,
                    "offset": offset,
                    "translatedLanguage[]": self.language,
                    "includeEmptyPages": 0,
                    "includeFuturePublishAt": 0,
                    "includeExternalUrl": 0,
                    "includeUnavailable": 0,
                    "includeFutureUpdates": 0,
                    "order[volume]": "asc",
                    "order[chapter]": "asc",
                    "includes[]": "scanlation_group",
                },
            )
            items = response.get("data") or []
            if not isinstance(items, list):
                raise RuntimeError("MangaDex returned an invalid chapter feed")

            for item in items:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                attributes = item.get("attributes") or {}
                if attributes.get("externalUrl") or attributes.get("isUnavailable"):
                    continue
                volume = str(attributes.get("volume") or "0")
                number = str(attributes.get("chapter") or f"0.{offset + len(chapters) + 1}")
                groups = [
                    relationship
                    for relationship in item.get("relationships") or []
                    if isinstance(relationship, dict) and relationship.get("type") == "scanlation_group"
                ]
                group_ids = sorted(str(group["id"]) for group in groups if group.get("id"))
                branch_id = "+".join(group_ids) or "no-group"
                key = (volume, number, branch_id)
                if key in seen:
                    continue
                seen.add(key)
                self._chapter_ids[key] = str(item["id"])

                group_names = [
                    str((group.get("attributes") or {}).get("name"))
                    for group in groups
                    if (group.get("attributes") or {}).get("name")
                ]
                chapter_title = attributes.get("title") or f"Chapter {number}"
                if group_names:
                    chapter_title = f"{chapter_title} [{', '.join(group_names)}]"
                chapters.append(
                    {
                        "id": item["id"],
                        "name": chapter_title,
                        "number": number,
                        "volume": volume,
                        "branches": [
                            {
                                "branch_id": branch_id,
                                "name": ", ".join(group_names) or "No group",
                            }
                        ],
                    }
                )

            next_offset = offset + len(items)
            total = int(response.get("total") or 0)
            if not items or next_offset >= total:
                break
            if next_offset <= offset:
                raise RuntimeError("MangaDex chapter pagination did not advance")
            offset = next_offset

        return chapters

    def get_chapter_content(
        self,
        slug: str,
        volume: str,
        number: str,
        branch_id: str | None = None,
        site_id: int = MANGADEX_SITE_ID,
        domain: str = "mangadex.org",
    ) -> dict[str, Any]:
        key = (str(volume), str(number), str(branch_id or "no-group"))
        chapter_id = self._chapter_ids.get(key)
        if chapter_id is None:
            self.get_novel_chapters(slug, site_id, domain)
            chapter_id = self._chapter_ids.get(key)
        if chapter_id is None:
            raise RuntimeError(f"MangaDex chapter {volume}/{number} was not found")

        response = self._request_json(f"/at-home/server/{chapter_id}", {"forcePort443": "true"})
        base_url = str(response.get("baseUrl") or "").rstrip("/")
        parsed_base = urlparse(base_url)
        chapter = response.get("chapter") or {}
        chapter_hash = chapter.get("hash")
        filenames = chapter.get("data") or []
        if parsed_base.scheme != "https" or not parsed_base.hostname or not chapter_hash or not filenames:
            raise RuntimeError(f"MangaDex chapter {chapter_id} returned invalid At-Home data")

        pages = [
            {
                "url": f"{base_url}/data/{chapter_hash}/{filename}",
                "image": str(filename),
            }
            for filename in filenames
        ]
        return {"id": chapter_id, "pages": pages}

    def get_image_servers(self, site_id: int = MANGADEX_SITE_ID, domain: str = "mangadex.org") -> list[str]:
        return []

    def report_image_result(
        self,
        url: str,
        success: bool,
        byte_count: int,
        duration_ms: int,
        cached: bool = False,
    ) -> None:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
        if hostname == "mangadex.org" or hostname.endswith(".mangadex.org"):
            return
        try:
            self.session.post(
                "https://api.mangadex.network/report",
                json={
                    "url": url,
                    "success": success,
                    "bytes": byte_count,
                    "duration": duration_ms,
                    "cached": cached,
                },
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "NetSanctum/0.1 (self-hosted MangaDex client)",
                },
                timeout=10,
            )
        except requests.RequestException as error:
            logger.debug("Failed to report MangaDex@Home result: %s", error)
