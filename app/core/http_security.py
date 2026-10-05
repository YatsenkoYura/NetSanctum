import secrets
from contextvars import ContextVar
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CROSS_SITE_CAPABILITY_PATHS = frozenset({"/alllib/api/save_token_external"})
PRIVATE_CAPABILITY_PREFIXES = ("/s/", "/tabletop/join/", "/tabletop/room/")

# A packaged browser extension sends `Origin: chrome-extension://<id>` (or the
# Gecko equivalent), which is neither same-origin nor an http(s) host, so the
# cross-site guard would reject it. Only these declared endpoints accept that
# origin, and every one of them authenticates with a bearer token rather than a
# cookie, so an ordinary web page still cannot reach them.
EXTENSION_ORIGIN_SCHEMES = frozenset({"chrome-extension", "moz-extension", "safari-web-extension"})
EXTENSION_CLIENT_PATHS = frozenset(
    {
        # Exchanges the bootstrap token for a bearer session. No cookie is read
        # or written here, so it carries no ambient authority to abuse.
        "/auth/login",
        "/api/vault/capture",
    }
)


def extension_origin_ids() -> frozenset[str]:
    """Extension ids this deployment accepts, from `NETSANCTUM_EXTENSION_ORIGIN_IDS`."""
    configured = (get_settings().NETSANCTUM_EXTENSION_ORIGIN_IDS or "").strip()
    if not configured:
        return frozenset()
    return frozenset(part.strip() for part in configured.split(",") if part.strip())


def _is_capability_route(path: str) -> bool:
    return (
        path in CROSS_SITE_CAPABILITY_PATHS
        or path.startswith("/tabletop/join/")
        or (path.startswith("/s/") and (path.endswith("/access") or path.endswith("/unlock")))
    )


def _is_extension_capability_route(path: str) -> bool:
    return path in EXTENSION_CLIENT_PATHS


def is_extension_origin(request: Request) -> bool:
    """True when the caller is a packaged browser extension on a declared route.

    The scheme alone identifies "some extension", not "our extension": any
    installed extension can put `chrome-extension://<its own id>` here. That is not
    a way in — both declared routes authenticate on a bearer token and read no
    cookie, so there is no ambient authority to borrow, and a non-browser client can
    drop the header entirely — but a check that accepts every extension is not a
    check. `NETSANCTUM_EXTENSION_ORIGIN_IDS` names the ids this deployment ships;
    an empty list keeps the permissive behaviour for an installation that has not
    set it, and a set list means an origin that is not on it is refused.
    """
    if not _is_extension_capability_route(request.url.path):
        return False
    origin = request.headers.get("origin", "")
    parsed = urlparse(origin)
    if parsed.scheme not in EXTENSION_ORIGIN_SCHEMES:
        return False
    allowed = extension_origin_ids()
    if not allowed:
        return True
    return parsed.netloc in allowed


def is_cross_site_request(request: Request) -> bool:
    """Reject browser cross-site mutations while leaving non-browser API clients usable."""
    if request.method not in UNSAFE_METHODS:
        return False
    if _is_capability_route(request.url.path):
        return False
    if is_extension_origin(request):
        return False

    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site == "cross-site":
        return True

    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        return True
    # Both names the request went by, because behind a reverse proxy they differ:
    # the browser addressed one host, the proxy forwarded another, and comparing
    # only the Host header rejected every mutation from the browser with a 403 that
    # looked like a CSRF block. `request.url.netloc` is only the forwarded value
    # when the deployment named its proxies in NETSANCTUM_TRUSTED_PROXY_IPS — which
    # is also what makes `X-Forwarded-Host` trustworthy here. Accepting either keeps
    # a genuinely foreign Origin rejected.
    candidates = {request.headers.get("host", "").lower(), request.url.netloc.lower()}
    candidates.discard("")
    return parsed.netloc.lower() not in candidates


# The Vault dashboard holds the per-tab unlock token in the page's memory and
# renders the contents of every vault it has open. That makes it the one page
# where a script injection is a total compromise, and it had no policy at all:
# the strict one below is applied only to share-capability responses.
#
# What this policy buys, and it is enforced today:
#   * no plugins (object-src 'none'), no base-tag hijack (base-uri 'none'),
#     no framing (frame-ancestors 'none'), no form posting anywhere but here
#     (form-action 'self');
#   * no network egress: connect-src, img-src and media-src are this origin
#     only, so an injected script cannot ship the token or a card's text to a
#     third party, and a remote og_image cannot phone home with the owner's IP
#     and visit timing;
#   * no eval. `script-src` allows this origin's files and the page's own inline
#     blocks, and nothing else.
#
# What it buys now, and this used to say otherwise. There was a time when this
# comment explained that a nonce could not be added yet: the tiles were built from
# ~80 inline `onclick`/`onerror` attributes, and a nonce would have voided every
# one of them — a dashboard where each button silently stops working, which looks
# like a bug in the seal rather than in the policy. The tiles were converted to
# delegated `data-net-action` attributes, `scripts/dashboard_inline_audit.py`
# fails the build if an inline handler or a non-nonced inline script reappears,
# and the nonce is minted per response below. A nonce covers `<script>` blocks
# only — it never covered event-handler *attributes*, which is why the conversion
# had to come first and not be skipped later.
DASHBOARD_CSP_PREFIXES = ("/vault/dashboard",)

# One nonce per response, minted where the header is written so the policy and the
# markup cannot disagree. A ContextVar rather than a request-state attribute
# because the template renders deep inside the router, and a global would be one
# tab's nonce leaking into another's page.
_CSP_NONCE: ContextVar[str] = ContextVar("csp_nonce", default="")


def csp_nonce() -> str:
    """The nonce for the response being rendered, for `<script nonce=...>`.

    Empty outside a dashboard request, where the policy has no nonce to honour.
    A template that renders it unconditionally is then no worse off than one that
    never had it.
    """
    return _CSP_NONCE.get()


def dashboard_csp(nonce: str) -> str:
    """The dashboard policy, carrying this response's nonce when it has one."""
    if not nonce:
        return DASHBOARD_CONTENT_SECURITY_POLICY
    return DASHBOARD_CONTENT_SECURITY_POLICY.replace(
        "script-src 'self'", f"script-src 'self' 'nonce-{nonce}'"
    )


DASHBOARD_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        # The type comes from Google Fonts, which is somebody else's host and so
        # has to be named: `fonts.googleapis.com` serves the stylesheet and
        # `fonts.gstatic.com` the woff2 files behind it. Self-hosting the two
        # families would let both origins go, and would stop every page view
        # telling Google which vault was opened — it needs the woff2 files, which
        # is a change of its own.
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        # `https:` and `http:` because a card's picture is often somebody else's:
        # `og_image` on a captured page is a remote address and `safeExternalUrl`
        # lets through exactly these two schemes. Without them the policy quietly
        # drops every remote thumbnail in the grid. What that costs is a request to
        # that host — the dashboard telling it which vault was opened — and what it
        # does not cost is execution: script-src stays this origin, and an `<img>`
        # cannot run anything. Self-hosting the pictures would remove both schemes
        # and that leak; it needs a fetch-and-store path, which is a change of its
        # own. Video stays strict: a vault's own files are served from here.
        "img-src 'self' data: blob: https: http:",
        "media-src 'self' blob:",
        "font-src 'self' https://fonts.gstatic.com",
        "connect-src 'self'",
        "form-action 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
    )
)


async def security_headers_middleware(request: Request, call_next):
    if is_cross_site_request(request):
        return JSONResponse({"detail": "Cross-site request rejected"}, status_code=403)

    nonce = ""
    token = None
    if request.url.path.startswith(DASHBOARD_CSP_PREFIXES):
        nonce = secrets.token_urlsafe(16)
        token = _CSP_NONCE.set(nonce)
    try:
        response = await call_next(request)
    finally:
        if token is not None:
            _CSP_NONCE.reset(token)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.path.startswith(PRIVATE_CAPABILITY_PREFIXES):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
    if request.url.path.startswith(DASHBOARD_CSP_PREFIXES):
        # The dashboard renders whatever its vaults hold, so it is not something
        # a shared cache should ever keep a copy of.
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Content-Security-Policy"] = dashboard_csp(nonce)
    # Conditional on the scheme, which is only correct once the deployment says
    # whether a proxy is in front. Behind a TLS-terminating proxy uvicorn sees
    # `http` unless it is told to trust `X-Forwarded-Proto`, and then this header
    # never went out at all — silently, because nothing failed.
    #
    # `includeSubDomains` is opt-in for a self-hosted deployment. It is a promise
    # about names this application knows nothing about: on `home.example.com` it
    # commits every sibling subdomain to HTTPS for a year, including ones serving
    # plain http on a home server that will never get a certificate. That is a much
    # worse failure than the header not being sent.
    if request.url.scheme == "https":
        policy = "max-age=31536000"
        if get_settings().NETSANCTUM_HSTS_INCLUDE_SUBDOMAINS:
            policy += "; includeSubDomains"
        response.headers.setdefault("Strict-Transport-Security", policy)
    return response
