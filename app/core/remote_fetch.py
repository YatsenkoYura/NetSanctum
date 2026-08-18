import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlparse, urlunsplit

import certifi
import requests
import urllib3
from requests.cookies import get_cookie_header

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class RemoteFetchError(ValueError):
    pass


def host_in_allowlist(hostname: str | None, allowed_hosts: set[str] | frozenset[str]) -> bool:
    host = (hostname or "").lower().rstrip(".")
    return bool(host) and any(host == allowed or host.endswith(f".{allowed}") for allowed in allowed_hosts)


def _public_addresses(hostname: str, port: int) -> list[str]:
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return []
    resolved = list(dict.fromkeys(str(address[4][0]) for address in addresses))
    return (
        resolved if resolved and all(ipaddress.ip_address(address).is_global for address in resolved) else []
    )


def validate_remote_url(
    url: str,
    *,
    allowed_hosts: set[str] | frozenset[str] | None = None,
    https_only: bool = True,
    resolve: bool = True,
) -> str:
    parsed = urlparse(url)
    allowed_schemes = {"https"} if https_only else {"http", "https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RemoteFetchError("Remote URL is not allowed")
    if allowed_hosts is not None and not host_in_allowlist(parsed.hostname, allowed_hosts):
        raise RemoteFetchError("Remote host is not allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if resolve and not _public_addresses(parsed.hostname, port):
        raise RemoteFetchError("Remote host does not resolve to a public address")
    return url


def fetch_bytes_checked(
    url: str,
    *,
    allowed_hosts: set[str] | frozenset[str] | None = None,
    auth_hosts: set[str] | frozenset[str] = frozenset(),
    headers: dict[str, str] | None = None,
    session: Any | None = None,
    max_redirects: int = 2,
    max_bytes: int = 8 * 1024 * 1024,
    allowed_content_prefixes: tuple[str, ...] = ("image/",),
    https_only: bool = True,
) -> tuple[bytes, str, str]:
    current_url = url
    request_headers = dict(headers or {})
    for redirect_count in range(max_redirects + 1):
        validate_remote_url(
            current_url,
            allowed_hosts=allowed_hosts,
            https_only=https_only,
            resolve=False,
        )
        parsed = urlparse(current_url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = _public_addresses(hostname, port)
        if not addresses:
            raise RemoteFetchError("Remote host does not resolve to a public address")

        hop_headers = dict(request_headers)
        hop_headers["Host"] = hostname if parsed.port is None else f"{hostname}:{port}"
        if not host_in_allowlist(hostname, auth_hosts):
            hop_headers.pop("Authorization", None)
            hop_headers.pop("authorization", None)
        if session is not None and "Cookie" not in hop_headers:
            prepared = requests.Request("GET", current_url).prepare()
            cookie_header = get_cookie_header(session.cookies, prepared)
            if cookie_header:
                hop_headers["Cookie"] = cookie_header

        pool_class = urllib3.HTTPSConnectionPool if parsed.scheme == "https" else urllib3.HTTPConnectionPool
        pool_kwargs = {
            "host": addresses[0],
            "port": port,
            "timeout": urllib3.Timeout(connect=5, read=15),
        }
        if parsed.scheme == "https":
            pool_kwargs.update(
                {
                    "server_hostname": hostname,
                    "assert_hostname": hostname,
                    "cert_reqs": "CERT_REQUIRED",
                    "ca_certs": certifi.where(),
                }
            )
        pool = pool_class(**pool_kwargs)
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        response = None
        try:
            response = pool.request(
                "GET",
                target,
                headers=hop_headers,
                redirect=False,
                preload_content=False,
                assert_same_host=False,
            )
            try:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location or redirect_count >= max_redirects:
                        raise RemoteFetchError("Remote redirect is not allowed")
                    current_url = urljoin(current_url, location)
                    continue
                if response.status != 200:
                    raise RemoteFetchError(f"Remote server returned HTTP {response.status}")

                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if allowed_content_prefixes and not any(
                    content_type.startswith(prefix) for prefix in allowed_content_prefixes
                ):
                    raise RemoteFetchError("Remote content type is not allowed")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise RemoteFetchError("Remote response is too large")

                content = bytearray()
                while chunk := response.read(64 * 1024, decode_content=True):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise RemoteFetchError("Remote response is too large")
                return bytes(content), content_type, current_url
            finally:
                response.release_conn()
        finally:
            pool.close()
    raise RemoteFetchError("Too many redirects")
