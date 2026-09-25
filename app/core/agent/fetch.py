"""Reading public web pages on the agent's behalf.

The safety rules are not reinvented here: scheme, credentials, public-IP resolution,
redirect count, byte budget and content type all come from app.core.remote_fetch.
This module only adds HTML-to-text conversion and a per-turn byte budget.
"""

import asyncio
import json
import re
from html.parser import HTMLParser

from app.core.agent.primitives import (
    FETCH_MAX_BYTES,
    FETCH_TEXT_LIMIT,
    AgentFetchResult,
)
from app.core.remote_fetch import RemoteFetchError, fetch_bytes_checked

FETCH_ALLOWED_CONTENT_PREFIXES = (
    "text/html",
    "text/plain",
    "text/markdown",
    "application/xhtml+xml",
    "application/json",
    "application/xml",
    "text/xml",
)
FETCH_TIMEOUT_SECONDS = 30
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class _ReadableTextParser(HTMLParser):
    """Collapse markup into the text a human would read, dropping script and style."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0
        self.title_parts: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        elif tag == "title":
            self.in_title = True
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1
        elif tag == "title":
            self.in_title = False
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.parts.append(data)

    def text(self) -> str:
        lines = (_WHITESPACE.sub(" ", line).strip() for line in "".join(self.parts).splitlines())
        return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()

    def title(self) -> str:
        return _WHITESPACE.sub(" ", "".join(self.title_parts)).strip()


def html_to_text(payload: str) -> tuple[str, str]:
    """Return (title, readable text) for an HTML document."""
    parser = _ReadableTextParser()
    parser.feed(payload)
    parser.close()
    return parser.title(), parser.text()


def plain_to_text(payload: str) -> tuple[str, str]:
    """Normalise plain text: collapse runs of spaces, keep paragraph breaks."""
    normalized = payload.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE.sub(" ", line).strip() for line in normalized.split("\n")]
    blocks: list[str] = []
    for line in lines:
        if line:
            blocks.append(line)
        elif blocks and blocks[-1]:
            blocks.append("")
    return "", "\n".join(blocks).strip()


def _to_text(payload: bytes, content_type: str) -> tuple[str, str]:
    text = payload.decode("utf-8", errors="replace")
    if content_type.startswith("application/json") or content_type.startswith(
        ("application/xml", "text/xml")
    ):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            pass
        return "", text
    if "html" in content_type:
        return html_to_text(text)
    return plain_to_text(text)


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    clipped = text[:max_chars]
    boundary = clipped.rfind("\n")
    if boundary > max_chars // 2:
        clipped = clipped[:boundary]
    return clipped.rstrip(), True


async def fetch_public_text(
    url: str,
    *,
    max_chars: int = FETCH_TEXT_LIMIT,
    max_bytes: int = FETCH_MAX_BYTES,
    session=None,
) -> AgentFetchResult:
    """Read one public page as bounded text.

    Raises RemoteFetchError for anything unsafe or unavailable; the caller decides how
    to report it, the model never sees raw exception text.
    """
    payload, content_type, final_url = await asyncio.wait_for(
        asyncio.to_thread(
            fetch_bytes_checked,
            url,
            max_bytes=max_bytes,
            allowed_content_prefixes=FETCH_ALLOWED_CONTENT_PREFIXES,
            https_only=True,
            session=session,
        ),
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    title, text = _to_text(payload, content_type)
    clipped, truncated = _clip(text, min(max_chars, FETCH_TEXT_LIMIT))
    return AgentFetchResult(
        url=url,
        final_url=final_url,
        title=title[:200],
        content_type=content_type[:120],
        text=clipped,
        truncated=truncated,
        bytes_read=len(payload),
    )


__all__ = [
    "FETCH_ALLOWED_CONTENT_PREFIXES",
    "RemoteFetchError",
    "fetch_public_text",
    "html_to_text",
    "plain_to_text",
]
