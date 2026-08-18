from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CROSS_SITE_CAPABILITY_PATHS = frozenset({"/alllib/api/save_token_external"})


def is_cross_site_request(request: Request) -> bool:
    """Reject browser cross-site mutations while leaving non-browser API clients usable."""
    if request.method not in UNSAFE_METHODS:
        return False
    if request.url.path in CROSS_SITE_CAPABILITY_PATHS:
        return False

    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site == "cross-site":
        return True

    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed = urlparse(origin)
    request_host = request.headers.get("host", "").lower()
    return parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != request_host


async def security_headers_middleware(request: Request, call_next):
    if is_cross_site_request(request):
        return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response
