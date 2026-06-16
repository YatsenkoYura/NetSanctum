"""
Unified API client and Prosemirror parser for all Lib Network sites.
"""

import html as html_lib
import logging
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import requests
from sqlalchemy import and_, select

from app.core.database import SyncSessionLocal
from app.modules.settings.models import Setting

logger = logging.getLogger(__name__)

REQUESTS_LIMIT = 5
REQUESTS_PERIOD = 1.0


class LibAPI:
    """Unified API client for all Lib Network sites (site_id: 1, 2, 3, 4, 5, 6)."""

    def __init__(self, auth_token: str | None = None):
        self.api_url = "https://api.cdnlibs.org/api/manga/"
        self.session = requests.Session()
        self.request_timestamps = deque()
        self._auth_token: str | None = auth_token if auth_token else self._load_token()

    def _load_token(self) -> str | None:
        """Load the Lib Network Bearer token from the settings DB (module scope: alllib)."""
        try:
            with SyncSessionLocal() as db:
                result = db.execute(
                    select(Setting.value).where(
                        and_(
                            Setting.scope == "module",
                            Setting.module_name == "alllib",
                            Setting.key == "lib_auth_token",
                        )
                    )
                )
                token = result.scalar_one_or_none()
                if token and token.strip():
                    logger.debug("Lib Network auth token loaded from settings.")
                    return token.strip()
        except Exception as e:
            logger.warning(f"Could not load lib_auth_token from settings: {e}")
        return None

    def get_site_info_from_url(self, url: str) -> tuple[int, str]:
        """Detect site_id and base domain from Lib URL.

        Confirmed site_id mapping (via api.cdnlibs.org):
          1 = MangaLib    (mangalib.me)
          2 = SlashLib    (slashlib.me) — BL/GL/yaoi/yuri
          3 = RanobeLib   (ranobelib.me)
          4 = HentaiLib   (hentailib.org / v2.hentailib.org) — explicit 18+ (RX)
        """
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()

        # Defaults
        site_id = 1
        domain = "mangalib.me"

        if "ranobelib" in netloc:
            site_id = 3
            domain = "ranobelib.me"
        elif "hentailib" in netloc:
            # HentaiLib (including v2.hentailib.org) = site_id 4
            site_id = 4
            domain = "hentailib.org"
        elif "slashlib" in netloc:
            # SlashLib = site_id 2 (BL/GL content)
            site_id = 2
            domain = "slashlib.me"
        elif "comixlib" in netloc:
            site_id = 5
            domain = "comixlib.me"
        elif "anilib" in netloc:
            site_id = 6
            domain = "anilib.me"
        elif "mangalib" in netloc:
            site_id = 1
            domain = "mangalib.me"

        return site_id, domain

    def extract_slug_from_url(self, url: str) -> str | None:
        """Extract the slug_url segment from a Lib URL.

        Handles these formats:
          /ru/manga/266434--happy-birthday-pushover-rabbit  → "266434--happy-birthday-pushover-rabbit"
          /ru/manga/chainsaw-man                            → "chainsaw-man"
          /ru/book/some-novel                               → "some-novel"
        The full slug_url (with ID prefix) is passed directly to the API,
        which resolves it correctly for all sites.
        """
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip("/").split("/")

        for i, part in enumerate(path_parts):
            if part in ("book", "manga", "anime") and i + 1 < len(path_parts):
                # Return the full segment (e.g. "266434--happy-birthday-pushover-rabbit")
                return path_parts[i + 1]

        if path_parts:
            return path_parts[-1]
        return None

    def make_request(
        self, url: str, params: dict | None = None, site_id: int = 1, domain: str = "mangalib.me"
    ) -> dict[str, Any]:
        """Execute request with rate limit and correct WAF-bypass headers."""
        self.wait_for_rate_limit()

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://{domain}/",
            "Origin": f"https://{domain}",
            "Site-Id": str(site_id),
        }
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        max_retries = 3
        delay = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=15)
                if resp.status_code == 200:
                    ct = resp.headers.get("Content-Type", "")
                    if "application/json" in ct or resp.text.strip().startswith("{"):
                        return resp.json()
                    else:
                        # HTML response — likely Cloudflare or auth-gated 404 page
                        logger.warning(
                            f"Non-JSON response from {url} (Content-Type: {ct}). Auth may be required."
                        )
                        return {}
                elif resp.status_code == 401 or resp.status_code == 403:
                    logger.error(
                        f"Auth required for {url} (HTTP {resp.status_code}). Site requires login for 18+ content."
                    )
                    return {"__auth_required": True}
                elif resp.status_code == 404:
                    ct = resp.headers.get("Content-Type", "")
                    if "application/json" in ct:
                        try:
                            return resp.json()
                        except Exception:
                            pass
                    logger.warning(f"404 for {url} — likely auth-gated or non-existent content.")
                    return {}
                elif resp.status_code in (502, 503, 504):
                    logger.warning(
                        f"Attempt {attempt}/{max_retries} received server error {resp.status_code} for {url}."
                    )
                    if attempt == max_retries:
                        logger.error(f"Request failed with status {resp.status_code}: {resp.text[:200]}")
                        return {}
                else:
                    logger.error(f"Request failed with status {resp.status_code}: {resp.text[:200]}")
                    return {}
            except Exception as e:
                logger.warning(f"Attempt {attempt}/{max_retries} failed for {url}: {e}")
                if attempt == max_retries:
                    logger.error(f"Network error after {max_retries} attempts: {e}")
                    return {}
            time.sleep(delay)
            delay *= 2

    def get_image_servers(self, site_id: int = 1, domain: str = "mangalib.me") -> list[str]:
        """Retrieve active CDN server base URLs dynamically from Lib Network constants."""
        constants_url = "https://api.cdnlibs.org/api/constants?fields[]=imageServers"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://{domain}/",
            "Origin": f"https://{domain}",
            "Site-Id": str(site_id),
        }
        try:
            resp = self.session.get(constants_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                servers = data.get("data", {}).get("imageServers", [])

                # Order servers prioritizing the 'compress' server first
                ordered = []
                for s in servers:
                    url = s.get("url")
                    if not url:
                        continue
                    if s.get("id") == "compress":
                        ordered.insert(0, url)
                    else:
                        ordered.append(url)
                return ordered
        except Exception as e:
            logger.error(f"Failed to fetch image servers: {e}")
        return ["https://img3.mixlib.me"]

    def get_novel_info(self, slug: str, site_id: int = 1, domain: str = "mangalib.me") -> dict[str, Any]:
        """Fetch general metadata for any media item."""
        fields = [
            "summary",
            "genres",
            "tags",
            "teams",
            "authors",
            "status_id",
            "artists",
            "format",
            "publisher",
        ]
        url_params = "&".join([f"fields[]={field}" for field in fields])

        if site_id == 6:
            # AnimeLib metadata API endpoint
            url = f"https://api.cdnlibs.org/api/anime/{slug}"
            # Pass site_id=5 for the API header
            data = self.make_request(url, site_id=5, domain=domain)
        else:
            url = f"{self.api_url}{slug}?{url_params}"
            data = self.make_request(url, site_id=site_id, domain=domain)

        return data.get("data", {})

    def get_novel_chapters(
        self, slug: str, site_id: int = 1, domain: str = "mangalib.me"
    ) -> list[dict[str, Any]]:
        """Fetch full chapter list or episodes list for any media item."""
        if site_id == 6:
            # AnimeLib episodes API endpoint
            url = "https://api.cdnlibs.org/api/episodes"
            data = self.make_request(url, params={"anime_id": slug}, site_id=5, domain=domain)
            episodes: list[dict[str, Any]] = data.get("data", [])

            # Map episodes to unified chapter schema
            mapped_episodes = []
            for ep in episodes:
                mapped_episodes.append(
                    {
                        "id": ep["id"],
                        "name": ep.get("name") or f"Episode {ep['number']}",
                        "number": str(ep["number"]),
                        "volume": str(ep.get("season") or "1"),
                        "branches": [{"branch_id": 0}],  # Stub to bypass branch filtering
                    }
                )
            return mapped_episodes

        url = f"{self.api_url}{slug}/chapters"
        data = self.make_request(url, site_id=site_id, domain=domain)
        chapters: list[dict[str, Any]] = data.get("data", [])

        # Filter out chapters on moderation
        filtered_chapters: list[dict[str, Any]] = []
        for chapter in chapters:
            branches = chapter.get("branches", [])
            is_on_moderation = any(
                isinstance(branch, dict) and branch.get("moderation", {}).get("id") == 0
                for branch in branches
            )
            if not is_on_moderation:
                filtered_chapters.append(chapter)

        return filtered_chapters

    def get_chapter_content(
        self,
        slug: str,
        volume: str,
        number: str,
        branch_id: str | None = None,
        site_id: int = 1,
        domain: str = "mangalib.me",
    ) -> dict[str, Any]:
        """Fetch pages or HTML text content for a single chapter."""
        url = f"{self.api_url}{slug}/chapter"
        params = {"volume": volume, "number": number}
        if branch_id and branch_id != "0":
            params["branch_id"] = branch_id

        data = self.make_request(url, params=params, site_id=site_id, domain=domain)
        return data.get("data", {})

    def get_episode_players(
        self, episode_id: int, site_id: int = 6, domain: str = "anilib.me"
    ) -> list[dict[str, Any]]:
        """Fetch player details for a specific anime episode by its ID."""
        url = f"https://api.cdnlibs.org/api/episodes/{episode_id}"
        # Use site_id=5 for the actual API call header
        data = self.make_request(url, site_id=5, domain=domain)
        return data.get("data", {}).get("players", [])

    def wait_for_rate_limit(self) -> None:
        """Rate limit coordinator."""
        current_time = time.monotonic()

        while self.request_timestamps and self.request_timestamps[0] < current_time - REQUESTS_PERIOD:
            self.request_timestamps.popleft()

        requests_in_period = len(self.request_timestamps)

        if requests_in_period + 1 <= REQUESTS_LIMIT:
            self.request_timestamps.append(time.monotonic())
            return

        if requests_in_period >= REQUESTS_LIMIT:
            wait_for_slot = self.request_timestamps[0] - (current_time - REQUESTS_PERIOD)
            if wait_for_slot > 0:
                time.sleep(wait_for_slot)

            current_time = time.monotonic()
            while self.request_timestamps and self.request_timestamps[0] < current_time - REQUESTS_PERIOD:
                self.request_timestamps.popleft()
            requests_in_period = len(self.request_timestamps)

        if self.request_timestamps:
            interval = REQUESTS_PERIOD / REQUESTS_LIMIT
            next_allowed_time = self.request_timestamps[-1] + interval
            wait_time = next_allowed_time - current_time
            if wait_time > 0:
                time.sleep(wait_time)

        self.request_timestamps.append(time.monotonic())


class LibParser:
    """Parses Prosemirror JSON nodes into HTML, routing image proxy paths to /alllib."""

    def __init__(self):
        self._element_handlers: dict[str, Callable[[dict[str, Any], list[dict[str, Any]]], str]] = {
            "hardBreak": self._handle_hard_break,
            "horizontalRule": self._handle_horizontal_rule,
            "image": self._handle_image,
            "paragraph": lambda element, attachments: self._handle_simple_tag(element, attachments, "p"),
            "orderedList": lambda element, attachments: self._handle_simple_tag(element, attachments, "ol"),
            "bulletList": lambda element, attachments: self._handle_simple_tag(element, attachments, "ul"),
            "listItem": lambda element, attachments: self._handle_simple_tag(element, attachments, "li"),
            "blockquote": lambda element, attachments: self._handle_simple_tag(
                element, attachments, "blockquote"
            ),
            "italic": lambda element, attachments: self._handle_simple_tag(element, attachments, "i"),
            "bold": lambda element, attachments: self._handle_simple_tag(element, attachments, "b"),
            "underline": lambda element, attachments: self._handle_simple_tag(element, attachments, "u"),
            "heading": lambda element, attachments: self._handle_simple_tag(element, attachments, "h2"),
            "text": self._handle_text,
        }

    def json_to_html(self, json_content: list[dict[str, Any]], attachments: list[dict[str, Any]]) -> str:
        """Convert JSON Prosemirror nodes to HTML string."""
        if not json_content:
            return ""

        html_parts = []
        for element in json_content:
            element_type = element.get("type")
            handler = self._handle_default
            if isinstance(element_type, str):
                handler = self._element_handlers.get(element_type, self._handle_default)
            html_parts.append(handler(element, attachments))

        return "".join(html_parts)

    def decode_html_entities(self, text: str, max_iterations: int = 5) -> str:
        """Recursively decode HTML entities."""
        if not isinstance(text, str):
            return text

        previous = text
        for _ in range(max_iterations):
            decoded = html_lib.unescape(previous)
            if decoded == previous:
                break
            previous = decoded
        return previous

    def _handle_simple_tag(self, element: dict[str, Any], attachments: list[dict[str, Any]], tag: str) -> str:
        """Handle simple elements containing child content."""
        content = (
            self.json_to_html(element.get("content", []), attachments) if element.get("content") else "<br>"
        )
        return f"<{tag}>{content}</{tag}>"

    def _handle_hard_break(self, element: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
        return "<br>"

    def _handle_horizontal_rule(self, element: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
        return "<hr>"

    def _handle_image(self, element: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
        import urllib.parse

        html = ""
        attrs = element.get("attrs", {})
        if attrs.get("images"):
            for img in attrs["images"]:
                image_id = img.get("image")
                file = next(
                    (f for f in attachments if f.get("name") == image_id or f.get("id") == image_id),
                    None,
                )
                if file and file.get("url"):
                    proxy_url = f"/alllib/api/proxy-image?url={urllib.parse.quote(file['url'])}"
                    html += f"<img src='{proxy_url}'>"
        elif attrs:
            src = attrs.get("src")
            if src:
                proxy_url = f"/alllib/api/proxy-image?url={urllib.parse.quote(src)}"
                attrs["src"] = proxy_url
            attr_str = " ".join([f'{key}="{value}"' for key, value in attrs.items() if value])
            html += f"<img {attr_str}>"
        return html

    def _handle_text(self, element: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
        text_val = element.get("text", "")
        processed_text = re.sub(" +", " ", text_val.replace("\n", " "))
        html = self.decode_html_entities(processed_text)

        # Apply marks if present
        marks = element.get("marks", [])
        for mark in marks:
            mark_type = mark.get("type")
            if mark_type == "bold":
                html = f"<b>{html}</b>"
            elif mark_type == "italic":
                html = f"<i>{html}</i>"
            elif mark_type == "underline":
                html = f"<u>{html}</u>"
            elif mark_type == "strike":
                html = f"<s>{html}</s>"
            elif mark_type == "code":
                html = f"<code>{html}</code>"
            elif mark_type == "link":
                href = mark.get("attrs", {}).get("href", "#")
                html = f"<a href='{href}' target='_blank' class='text-teal-400 hover:text-teal-300 underline'>{html}</a>"
        return html

    def _handle_default(self, element: dict[str, Any], attachments: list[dict[str, Any]]) -> str:
        if "content" in element:
            return self.json_to_html(element["content"], attachments)
        return ""
