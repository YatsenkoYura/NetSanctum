"""
RanobeLib API client and Content Parser.
"""

import html as html_lib
import json
import re
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional
from urllib.parse import urlparse

import requests

REQUESTS_LIMIT = 90
REQUESTS_PERIOD = 60
REQUEST_TIMEOUT = 15
RETRY_DELAYS = [3, 3, 10, 10]


class OperationCancelledError(Exception):
    """Exception thrown when operation is cancelled."""


class RanobeLibAPI:
    """RanobeLib API Client wrapper."""

    def __init__(self):
        self.api_url = "https://api.cdnlibs.org/api/manga/"
        self.site_url = "https://ranobelib.me"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Origin": self.site_url,
                "Referer": f"{self.site_url}/",
                "Site-Id": "3",
            }
        )
        self.request_timestamps: Deque[float] = deque()

    def make_request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        retry: bool = True,
    ) -> Dict[str, Any]:
        """Perform request to API with rate-limiting controls and error retries."""
        self.wait_for_rate_limit()

        if not retry:
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                return response.json()
            except Exception:
                return {}

        # Retry loop
        for i, delay in enumerate(RETRY_DELAYS):
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if response.status_code == 404:
                    try:
                        return response.json()
                    except Exception:
                        return {}
                response.raise_for_status()
                return response.json()
            except Exception as e:
                is_last_attempt = i == len(RETRY_DELAYS) - 1
                if is_last_attempt:
                    raise e
                time.sleep(delay)

        return {}

    def extract_slug_from_url(self, url: str) -> Optional[str]:
        """Extract slug from RanobeLib URL."""
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.strip("/").split("/")

        # Check: /ru/book/slug or /book/slug or just slug
        for i, part in enumerate(path_parts):
            if part == "book" and i + 1 < len(path_parts):
                return path_parts[i + 1]

        # fallback: return the last part of URL path
        if path_parts:
            # remove any prefix ID if it's like 12345--slug
            last = path_parts[-1]
            if "--" in last:
                return last.split("--")[-1]
            return last
        return None

    def get_novel_info(self, slug: str) -> Dict[str, Any]:
        """Get light novel metadata."""
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
        url = f"{self.api_url}{slug}?{url_params}"

        data = self.make_request(url)
        return data.get("data", {})

    def get_novel_chapters(self, slug: str) -> List[Dict[str, Any]]:
        """Get list of chapters for a light novel."""
        url = f"{self.api_url}{slug}/chapters"
        data = self.make_request(url)
        chapters: List[Dict[str, Any]] = data.get("data", [])

        # Filter out chapters on moderation
        filtered_chapters: List[Dict[str, Any]] = []
        for chapter in chapters:
            branches = chapter.get("branches", [])
            is_on_moderation = any(
                isinstance(branch, dict)
                and branch.get("moderation", {}).get("id") == 0
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
        branch_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get chapter content."""
        url = f"{self.api_url}{slug}/chapter"
        params = {"volume": volume, "number": number}
        if branch_id and branch_id != "0":
            params["branch_id"] = branch_id

        data = self.make_request(url, params=params)
        return data.get("data", {})

    def wait_for_rate_limit(self) -> None:
        """Rate limit handler."""
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


class RanobeLibParser:
    """Parses RanobeLib Prosemirror JSON nodes into HTML."""

    def __init__(self):
        self._element_handlers: Dict[str, Callable[[Dict[str, Any], List[Dict[str, Any]]], str]] = {
            "hardBreak": self._handle_hard_break,
            "horizontalRule": self._handle_horizontal_rule,
            "image": self._handle_image,
            "paragraph": lambda element, attachments: self._handle_simple_tag(
                element, attachments, "p"
            ),
            "orderedList": lambda element, attachments: self._handle_simple_tag(
                element, attachments, "ol"
            ),
            "listItem": lambda element, attachments: self._handle_simple_tag(
                element, attachments, "li"
            ),
            "blockquote": lambda element, attachments: self._handle_simple_tag(
                element, attachments, "blockquote"
            ),
            "italic": lambda element, attachments: self._handle_simple_tag(element, attachments, "i"),
            "bold": lambda element, attachments: self._handle_simple_tag(element, attachments, "b"),
            "underline": lambda element, attachments: self._handle_simple_tag(
                element, attachments, "u"
            ),
            "heading": lambda element, attachments: self._handle_simple_tag(
                element, attachments, "h2"
            ),
            "text": self._handle_text,
        }

    def json_to_html(
        self, json_content: List[Dict[str, Any]], attachments: List[Dict[str, Any]]
    ) -> str:
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

    def _handle_simple_tag(
        self, element: Dict[str, Any], attachments: List[Dict[str, Any]], tag: str
    ) -> str:
        """Handle simple elements containing child content."""
        content = (
            self.json_to_html(element.get("content", []), attachments)
            if element.get("content")
            else "<br>"
        )
        return f"<{tag}>{content}</{tag}>"

    def _handle_hard_break(self, element: Dict[str, Any], attachments: List[Dict[str, Any]]) -> str:
        return "<br>"

    def _handle_horizontal_rule(
        self, element: Dict[str, Any], attachments: List[Dict[str, Any]]
    ) -> str:
        return "<hr>"

    def _handle_image(self, element: Dict[str, Any], attachments: List[Dict[str, Any]]) -> str:
        import urllib.parse
        html = ""
        attrs = element.get("attrs", {})
        if attrs.get("images"):
            for img in attrs["images"]:
                image_id = img.get("image")
                file = next(
                    (
                        f
                        for f in attachments
                        if f.get("name") == image_id or f.get("id") == image_id
                    ),
                    None,
                )
                if file and file.get("url"):
                    proxy_url = f"/ranobelib/api/proxy-image?url={urllib.parse.quote(file['url'])}"
                    html += f"<img src='{proxy_url}'>"
        elif attrs:
            src = attrs.get("src")
            if src:
                proxy_url = f"/ranobelib/api/proxy-image?url={urllib.parse.quote(src)}"
                attrs["src"] = proxy_url
            attr_str = " ".join([f'{key}="{value}"' for key, value in attrs.items() if value])
            html += f"<img {attr_str}>"
        return html

    def _handle_text(self, element: Dict[str, Any], attachments: List[Dict[str, Any]]) -> str:
        text_val = element.get("text", "")
        processed_text = re.sub(" +", " ", text_val.replace("\n", " "))
        return self.decode_html_entities(processed_text)

    def _handle_default(self, element: Dict[str, Any], attachments: List[Dict[str, Any]]) -> str:
        return f"<pre>{json.dumps(element, indent=2)}</pre>"
