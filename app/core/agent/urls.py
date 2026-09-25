"""Provider URL handling shared by every outbound call.

A saved provider URL is almost always a base (``https://host/v1``), while providers
speak full endpoints (``/v1/chat/completions``). Completing the path here keeps every
call site from having to remember.
"""

from urllib.parse import urlsplit, urlunsplit

CHAT_COMPLETIONS = "/chat/completions"
TRANSCRIPTIONS = "/audio/transcriptions"
SPEECH = "/audio/speech"


def provider_endpoint(url: str, endpoint: str) -> str:
    """Append the endpoint to a base URL, or leave an already complete URL alone."""
    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    path = parsed.path.rstrip("/")
    if not path.endswith(endpoint):
        path = f"{path}{endpoint}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


__all__ = [
    "CHAT_COMPLETIONS",
    "SPEECH",
    "TRANSCRIPTIONS",
    "provider_endpoint",
]
