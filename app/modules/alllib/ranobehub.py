"""RanobeHub source adapter for the shared alllib download pipeline."""

import logging
import re
import time
from collections import deque
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

RANOBEHUB_SITE_ID = 7
RANOBEHUB_API_BASE = "https://ranobe.space"
RANOBEHUB_HOSTS = {"ranobehub.org", "ranobe.space"}
_ALLOWED_CONTENT_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "div",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "sub",
    "sup",
    "u",
    "ul",
}
_VOID_CONTENT_TAGS = {"br", "hr", "img"}
_BLOCKED_CONTENT_TAGS = {"iframe", "object", "script", "style", "template"}


def is_ranobehub_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in RANOBEHUB_HOSTS)


class _BookPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.description: str | None = None
        self._inside_h1 = False
        self._h1_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta":
            property_name = attributes.get("property") or attributes.get("name")
            content = attributes.get("content")
            if property_name == "og:title" and content:
                self.title = content
            elif property_name in {"og:description", "description"} and content and not self.description:
                self.description = content
        elif tag == "h1" and not self.title:
            self._inside_h1 = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._inside_h1:
            self._inside_h1 = False
            title = "".join(self._h1_parts).strip()
            if title:
                self.title = title

    def handle_data(self, data: str) -> None:
        if self._inside_h1:
            self._h1_parts.append(data)


class _ChapterHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _BLOCKED_CONTENT_TAGS:
            self.blocked_depth += 1
            return
        if self.blocked_depth or tag not in _ALLOWED_CONTENT_TAGS:
            return

        attributes = dict(attrs)
        safe_attributes: list[tuple[str, str]] = []
        if tag == "img":
            src = attributes.get("src")
            if src:
                absolute_src = urljoin(RANOBEHUB_API_BASE, src)
                if is_ranobehub_url(absolute_src) and urlparse(absolute_src).scheme == "https":
                    safe_attributes.append(("src", absolute_src))
            for name in ("alt", "title"):
                if attributes.get(name):
                    safe_attributes.append((name, attributes[name] or ""))
            if not any(name == "src" for name, _ in safe_attributes):
                return
        elif tag == "a":
            href = attributes.get("href")
            if href:
                parsed_href = urlparse(href)
                if parsed_href.scheme in {"http", "https"}:
                    safe_attributes.extend(
                        [("href", href), ("target", "_blank"), ("rel", "noopener noreferrer")]
                    )
        elif tag in {"ol", "li"}:
            for name in ("start", "value"):
                value = attributes.get(name)
                if value and value.lstrip("-").isdigit():
                    safe_attributes.append((name, value))

        attr_text = "".join(f' {name}="{_escape_attribute(value)}"' for name, value in safe_attributes)
        suffix = " /" if tag in _VOID_CONTENT_TAGS else ""
        self.parts.append(f"<{tag}{attr_text}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _BLOCKED_CONTENT_TAGS:
            if self.blocked_depth:
                self.blocked_depth -= 1
            return
        if not self.blocked_depth and tag in _ALLOWED_CONTENT_TAGS and tag not in _VOID_CONTENT_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.parts.append(_escape_text(data))


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attribute(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;").replace("'", "&#x27;")


def sanitize_chapter_html(content: str) -> str:
    sanitizer = _ChapterHTMLSanitizer()
    sanitizer.feed(content)
    sanitizer.close()
    return "".join(sanitizer.parts)


class RanobeHubAPI:
    """Adapt RanobeHub's public API to the interface consumed by alllib tasks."""

    def __init__(self, auth_token: str | None = None):
        self.session = requests.Session()
        self._auth_token = auth_token
        self.request_timestamps: deque[float] = deque()
        self._books: dict[str, dict[str, Any]] = {}
        self._chapter_ids: dict[tuple[int, str, str, str], int] = {}

    def get_site_info_from_url(self, url: str) -> tuple[int, str]:
        return RANOBEHUB_SITE_ID, "ranobe.space"

    def extract_slug_from_url(self, url: str) -> str | None:
        path_parts = urlparse(url).path.strip("/").split("/")
        for index, part in enumerate(path_parts):
            if part == "ranobe" and index + 1 < len(path_parts):
                return path_parts[index + 1]
        return None

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": f"{RANOBEHUB_API_BASE}/",
        }
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        while self.request_timestamps and self.request_timestamps[0] <= now - 1:
            self.request_timestamps.popleft()
        if len(self.request_timestamps) >= 5:
            time.sleep(max(0, self.request_timestamps[0] + 1 - now))
        self.request_timestamps.append(time.monotonic())

    def _request(self, path: str, params: dict[str, Any] | None = None, *, accept: str = "application/json"):
        self._wait_for_rate_limit()
        url = urljoin(f"{RANOBEHUB_API_BASE}/", path.lstrip("/"))
        delay = 1.0
        for attempt in range(3):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=self._headers(accept),
                    timeout=20,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    content_type = response.headers.get("content-type", "").lower()
                    if accept == "application/json" and "json" not in content_type:
                        if attempt == 2:
                            raise RuntimeError(f"RanobeHub returned a non-JSON response for {url}")
                    else:
                        return response
                if response.status_code not in {429, 502, 503, 504}:
                    response.raise_for_status()
            except requests.RequestException:
                if attempt == 2:
                    raise
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
        raise RuntimeError(f"RanobeHub request failed: {url}")

    def _request_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(path, params)
        try:
            data = response.json()
        except ValueError as error:
            raise RuntimeError(f"RanobeHub returned invalid JSON for {response.url}") from error
        if not isinstance(data, dict):
            raise RuntimeError(f"RanobeHub returned an unexpected response for {response.url}")
        return data

    def _resolve_book(self, reference: str) -> dict[str, Any]:
        if reference in self._books:
            return self._books[reference]

        safe_reference = quote(reference, safe="-")
        page = self._request(f"/ranobe/{safe_reference}", accept="text/html")
        final_reference = urlparse(page.url).path.rstrip("/").split("/")[-1]
        parser = _BookPageParser()
        parser.feed(page.text)
        if not parser.title:
            raise RuntimeError("Could not determine the RanobeHub title")

        search_data = self._request_json("/api/search", {"q": parser.title})
        books = [book for book in search_data.get("books", []) if isinstance(book, dict)]
        id_match = re.match(r"^(\d+)(?:-|$)", final_reference)
        book_id = int(id_match.group(1)) if id_match else None

        book = next((item for item in books if book_id is not None and item.get("id") == book_id), None)
        if book is None:
            book = next(
                (item for item in books if item.get("slug") in {reference, final_reference}),
                None,
            )
        if book is None:
            exact_title = parser.title.casefold()
            book = next(
                (item for item in books if str(item.get("title", "")).casefold() == exact_title), None
            )
        if book is None:
            raise RuntimeError(f"Could not resolve RanobeHub book {reference!r}")

        resolved = dict(book)
        resolved["page_description"] = parser.description
        resolved["reference"] = final_reference
        self._books[reference] = resolved
        self._books[final_reference] = resolved
        return resolved

    def get_novel_info(
        self, slug: str, site_id: int = RANOBEHUB_SITE_ID, domain: str = "ranobe.space"
    ) -> dict[str, Any]:
        book = self._resolve_book(slug)
        poster_url = book.get("posterUrl")
        cover = urljoin(f"{RANOBEHUB_API_BASE}/", poster_url.lstrip("/")) if poster_url else None
        tags = book.get("tags") or []
        return {
            "id": book.get("id"),
            "slug": book.get("reference") or slug,
            "rus_name": book.get("title"),
            "eng_name": book.get("originalTitle"),
            "summary": book.get("description") or book.get("page_description") or "",
            "year": book.get("year"),
            "status": book.get("status"),
            "rating": book.get("rating"),
            "tags": [tag if isinstance(tag, dict) else {"name": str(tag)} for tag in tags],
            "cover": {"default": cover} if cover else None,
        }

    def get_novel_chapters(
        self, slug: str, site_id: int = RANOBEHUB_SITE_ID, domain: str = "ranobe.space"
    ) -> list[dict[str, Any]]:
        book = self._resolve_book(slug)
        book_id = int(book["id"])
        chapters: list[dict[str, Any]] = []
        offset = 0

        while True:
            data = self._request_json(
                f"/api/books/{book_id}/chapters",
                {"sort": "asc", "offset": offset, "limit": 250},
            )
            items = data.get("items", [])
            if not isinstance(items, list):
                raise RuntimeError("RanobeHub returned an invalid chapter list")

            for item in items:
                if not isinstance(item, dict) or item.get("id") is None:
                    continue
                volume = str(item.get("volume") or "0")
                number = str(item.get("number") or "0")
                options = item.get("translationOptions") or []
                branches = [
                    {
                        "branch_id": str(option.get("slug") or "main"),
                        "name": option.get("name"),
                        "is_default": bool(option.get("isDefault")),
                        "is_preferred": bool(option.get("isPreferred")),
                    }
                    for option in options
                    if isinstance(option, dict)
                ]
                if not branches:
                    branches = [{"branch_id": "main", "name": "Основной перевод"}]

                chapter_id = int(item["id"])
                for branch in branches:
                    self._chapter_ids[(book_id, volume, number, branch["branch_id"])] = chapter_id
                chapters.append(
                    {
                        "id": chapter_id,
                        "name": item.get("title"),
                        "number": number,
                        "volume": volume,
                        "branches": branches,
                    }
                )

            next_offset = data.get("nextOffset")
            if next_offset is None:
                break
            next_offset = int(next_offset)
            if next_offset <= offset:
                raise RuntimeError("RanobeHub chapter pagination did not advance")
            offset = next_offset

        return chapters

    def get_chapter_content(
        self,
        slug: str,
        volume: str,
        number: str,
        branch_id: str | None = None,
        site_id: int = RANOBEHUB_SITE_ID,
        domain: str = "ranobe.space",
    ) -> dict[str, Any]:
        book = self._resolve_book(slug)
        book_id = int(book["id"])
        branch = branch_id or "main"
        chapter_id = self._chapter_ids.get((book_id, str(volume), str(number), branch))
        if chapter_id is None:
            self.get_novel_chapters(slug, site_id, domain)
            chapter_id = self._chapter_ids.get((book_id, str(volume), str(number), branch))
        if chapter_id is None:
            raise RuntimeError(f"RanobeHub chapter {volume}/{number} was not found")

        data = self._request_json(f"/api/chapters/{chapter_id}", {"translation": branch})
        chapter = data.get("chapter")
        if not isinstance(chapter, dict):
            raise RuntimeError(f"RanobeHub chapter {chapter_id} is unavailable")
        if chapter.get("paid") and not chapter.get("entitled"):
            raise RuntimeError(f"RanobeHub chapter {chapter_id} requires access")

        selected_branch = chapter.get("translationBranch") or {}
        selected_slug = selected_branch.get("slug")
        if branch != "main" and selected_slug and selected_slug != branch:
            raise RuntimeError(f"RanobeHub translation {branch!r} is unavailable for chapter {chapter_id}")

        content = chapter.get("html") or ""
        if not isinstance(content, str):
            raise RuntimeError(f"RanobeHub chapter {chapter_id} returned invalid HTML")
        content = re.sub(
            r"(?P<prefix>\bsrc\s*=\s*['\"])(?P<url>/[^'\"]+)",
            lambda match: f"{match.group('prefix')}{urljoin(RANOBEHUB_API_BASE, match.group('url'))}",
            content,
            flags=re.IGNORECASE,
        )
        content = sanitize_chapter_html(content)
        return {
            "id": chapter_id,
            "content": content,
            "attachments": [],
            "title": chapter.get("title"),
            "volume": str(chapter.get("volume") or volume),
            "number": str(chapter.get("number") or number),
        }


def get_source_api(url: str, auth_token: str | None = None) -> Any:
    if is_ranobehub_url(url):
        return RanobeHubAPI(auth_token=auth_token)

    from app.modules.alllib.mangadex import MangaDexAPI, is_mangadex_url

    if is_mangadex_url(url):
        return MangaDexAPI(auth_token=auth_token)

    from app.modules.alllib.novelbin import NovelBinAPI, is_novelbin_url

    if is_novelbin_url(url):
        return NovelBinAPI(url, auth_token=auth_token)

    from app.modules.alllib.api import LibAPI

    return LibAPI(auth_token=auth_token)
