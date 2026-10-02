"""Deep links MIKU may send the client to.

Every module invents its own `?miku_item` parameter and its own dashboard route.
Without a pinned contract a renamed route silently turns the assistant's Open
buttons into 404s. This table is that contract: the only paths the chat may
navigate to, and the test that keeps them honest.
"""

from urllib.parse import parse_qsl, urlparse

# Module -> allowed path prefixes for `open_path` documents publish.
OPEN_PATH_PREFIXES: dict[str, tuple[str, ...]] = {
    "alllib": ("/alllib/reader/", "/alllib/dashboard"),
    "music": ("/music/dashboard",),
    "planner": ("/planner",),
    "vault": ("/vault/dashboard",),
    "video_archiver": ("/video-archiver/dashboard",),
    "youtube": ("/youtube/watch/", "/youtube/dashboard"),
}


def is_allowed_open_path(module_id: str, open_path: str | None) -> bool:
    """True when the path is local, absolute, and belongs to the module's routes."""
    if not open_path:
        return False
    parsed = urlparse(open_path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return False
    if "\\" in open_path or ".." in parsed.path.split("/"):
        return False
    prefixes = OPEN_PATH_PREFIXES.get(module_id)
    if not prefixes:
        return False
    if not open_path.startswith(prefixes):
        return False
    # Query strings must be plain key=value pairs, no javascript: payloads.
    for _, value in parse_qsl(parsed.query, keep_blank_values=True):
        if value.strip().casefold().startswith(("javascript:", "data:")):
            return False
    return True


__all__ = ["OPEN_PATH_PREFIXES", "is_allowed_open_path"]
