"""NovelBin HTML adapter for the shared AllLib download pipeline."""

import html
import logging
import re
import time
from collections import deque
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)

NOVELBIN_SITE_ID = 9
NOVELBIN_HOSTS = {"novel-bin.net", "novel-bin.com"}
_VOID_TAGS = {"br", "hr", "img"}
_ALLOWED_TAGS = {
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
_BLOCKED_TAGS = {"iframe", "object", "script", "style", "template"}


def is_novelbin_url(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in NOVELBIN_HOSTS)


class _NovelPageParser(HTMLParser):
    def __init__(self, slug: str) -> None:
        super().__init__(convert_charrefs=True)
        self.slug = slug
        self.metadata: dict[str, str] = {}
        self.chapters: list[tuple[str, str]] = []
        self.description_parts: list[str] = []
        self._description_depth = 0
        self._chapter_paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if self._description_depth:
            if tag not in _VOID_TAGS:
                self._description_depth += 1
        elif "desc-text" in classes and attributes.get("itemprop") == "description":
            self._description_depth = 1
        if tag == "meta":
            key = attributes.get("property") or attributes.get("name")
            value = attributes.get("content")
            if key and value:
                self.metadata[key] = value
            return
        if tag != "a" or not attributes.get("href"):
            return

        href = attributes["href"] or ""
        parsed = urlparse(href)
        expected_prefix = f"/novel-bin/{self.slug}/chapter-"
        if parsed.hostname and not is_novelbin_url(href):
            return
        if not parsed.path.startswith(expected_prefix) or parsed.path in self._chapter_paths:
            return
        self._chapter_paths.add(parsed.path)
        self.chapters.append((href, attributes.get("title") or parsed.path.rsplit("/", 1)[-1]))

    def handle_endtag(self, tag: str) -> None:
        if self._description_depth and tag not in _VOID_TAGS:
            self._description_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._description_depth and data.strip():
            self.description_parts.append(data.strip())


class _ChapterContentParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.depth = 0
        self.blocked_depth = 0

    @property
    def active(self) -> bool:
        return self.depth > 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if not self.active:
            if tag == "div" and attributes.get("id") == "chr-content":
                self.depth = 1
            return

        if tag not in _VOID_TAGS:
            self.depth += 1
        if tag in _BLOCKED_TAGS:
            self.blocked_depth += 1
            return
        if self.blocked_depth or tag not in _ALLOWED_TAGS:
            return

        safe_attributes: list[tuple[str, str]] = []
        if tag == "img":
            src = attributes.get("src") or attributes.get("data-src")
            absolute_src = urljoin(self.base_url, src) if src else ""
            if not is_novelbin_url(absolute_src) or urlparse(absolute_src).scheme != "https":
                return
            safe_attributes.append(("src", absolute_src))
            for name in ("alt", "title"):
                if attributes.get(name):
                    safe_attributes.append((name, attributes[name] or ""))
        elif tag == "a":
            href = attributes.get("href")
            if href:
                absolute_href = urljoin(self.base_url, href)
                if urlparse(absolute_href).scheme == "https":
                    safe_attributes.extend(
                        [("href", absolute_href), ("target", "_blank"), ("rel", "noopener noreferrer")]
                    )
        elif tag in {"ol", "li"}:
            for name in ("start", "value"):
                value = attributes.get(name)
                if value and value.lstrip("-").isdigit():
                    safe_attributes.append((name, value))

        attr_text = "".join(f' {name}="{html.escape(value, quote=True)}"' for name, value in safe_attributes)
        suffix = " /" if tag in _VOID_TAGS else ""
        self.parts.append(f"<{tag}{attr_text}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self.active:
            return
        if self.depth == 1 and tag == "div":
            self.depth = 0
            return
        if tag in _BLOCKED_TAGS:
            if self.blocked_depth:
                self.blocked_depth -= 1
        elif not self.blocked_depth and tag in _ALLOWED_TAGS and tag not in _VOID_TAGS:
            self.parts.append(f"</{tag}>")

        if tag not in _VOID_TAGS:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.active and not self.blocked_depth:
            self.parts.append(html.escape(data))


class NovelBinAPI:
    """Scrape NovelBin's server-rendered title and chapter pages."""

    def __init__(self, source_url: str, auth_token: str | None = None):
        self.session = requests.Session()
        self._auth_token = auth_token
        self.request_timestamps: deque[float] = deque()
        self.domain = "novel-bin.net"
        self.base_url = "https://novel-bin.net"
        self._books: dict[str, _NovelPageParser] = {}
        self._chapter_urls: dict[tuple[str, str, str], str] = {}
        self.get_site_info_from_url(source_url)

    def get_site_info_from_url(self, url: str) -> tuple[int, str]:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
        if not any(hostname == host or hostname.endswith(f".{host}") for host in NOVELBIN_HOSTS):
            raise ValueError("Unsupported NovelBin URL")
        self.domain = hostname.removeprefix("www.")
        self.base_url = f"https://{self.domain}"
        return NOVELBIN_SITE_ID, self.domain

    def extract_slug_from_url(self, url: str) -> str | None:
        parts = urlparse(url).path.strip("/").split("/")
        for index, part in enumerate(parts):
            if part == "novel-bin" and index + 1 < len(parts):
                return parts[index + 1]
        return None

    def _wait_for_rate_limit(self) -> None:
        now = time.monotonic()
        while self.request_timestamps and self.request_timestamps[0] <= now - 1:
            self.request_timestamps.popleft()
        if len(self.request_timestamps) >= 2:
            time.sleep(max(0, self.request_timestamps[0] + 1 - now))
        self.request_timestamps.append(time.monotonic())

    def _request_html(self, path: str) -> str:
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        delay = 1.0
        for attempt in range(3):
            self._wait_for_rate_limit()
            try:
                response = self.session.get(
                    url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Referer": f"{self.base_url}/",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        ),
                    },
                    timeout=20,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    if not is_novelbin_url(response.url):
                        raise RuntimeError("NovelBin redirected to an unsupported host")
                    return response.text
                if response.status_code not in {429, 502, 503, 504}:
                    response.raise_for_status()
            except requests.RequestException:
                if attempt == 2:
                    raise
            if attempt < 2:
                time.sleep(delay)
                delay *= 2
        raise RuntimeError(f"NovelBin request failed: {url}")

    def _get_book(self, slug: str) -> _NovelPageParser:
        if slug not in self._books:
            parser = _NovelPageParser(slug)
            parser.feed(self._request_html(f"/novel-bin/{slug}/"))
            parser.close()
            if not parser.metadata.get("og:novel:novel_name") and not parser.metadata.get("og:title"):
                raise RuntimeError(f"Could not determine NovelBin title {slug!r}")
            self._books[slug] = parser
        return self._books[slug]

    def get_novel_info(
        self, slug: str, site_id: int = NOVELBIN_SITE_ID, domain: str = "novel-bin.net"
    ) -> dict[str, Any]:
        book = self._get_book(slug)
        metadata = book.metadata
        title = metadata.get("og:novel:novel_name") or metadata.get("og:title") or slug
        cover = metadata.get("og:image")
        if cover:
            cover = urljoin(f"{self.base_url}/", cover)
        author = metadata.get("og:novel:author")
        genre = metadata.get("og:novel:genre")
        description = (
            " ".join(book.description_parts)
            or metadata.get("description")
            or metadata.get("og:description", "")
        )
        return {
            "id": slug,
            "name": title,
            "eng_name": title,
            "summary": html.escape(description),
            "status": metadata.get("og:novel:status"),
            "authors": [{"name": author}] if author else [],
            "tags": [{"name": genre}] if genre else [],
            "cover": {"default": cover} if cover else None,
        }

    def get_novel_chapters(
        self, slug: str, site_id: int = NOVELBIN_SITE_ID, domain: str = "novel-bin.net"
    ) -> list[dict[str, Any]]:
        book = self._get_book(slug)
        chapters: list[dict[str, Any]] = []
        for index, (href, title) in enumerate(book.chapters, start=1):
            path = urlparse(href).path.rstrip("/")
            match = re.search(r"/chapter-(\d+(?:\.\d+)?)$", path)
            number = match.group(1) if match else str(index)
            self._chapter_urls[("0", number, "0")] = urljoin(f"{self.base_url}/", href)
            chapters.append(
                {
                    "id": path.rsplit("/", 1)[-1],
                    "name": title,
                    "number": number,
                    "volume": "0",
                    "branches": [{"branch_id": "0"}],
                }
            )
        return chapters

    def get_chapter_content(
        self,
        slug: str,
        volume: str,
        number: str,
        branch_id: str | None = None,
        site_id: int = NOVELBIN_SITE_ID,
        domain: str = "novel-bin.net",
    ) -> dict[str, Any]:
        key = (str(volume), str(number), str(branch_id or "0"))
        chapter_url = self._chapter_urls.get(key)
        if chapter_url is None:
            self.get_novel_chapters(slug, site_id, domain)
            chapter_url = self._chapter_urls.get(key)
        if chapter_url is None:
            raise RuntimeError(f"NovelBin chapter {volume}/{number} was not found")

        parser = _ChapterContentParser(self.base_url)
        parser.feed(self._request_html(urlparse(chapter_url).path))
        parser.close()
        content = "".join(parser.parts).strip()
        if not content:
            raise RuntimeError(f"NovelBin chapter {volume}/{number} returned no content")
        return {"id": number, "content": content, "attachments": []}
