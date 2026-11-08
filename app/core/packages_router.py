import asyncio
import hashlib
import logging
import re
import tempfile
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.database import AsyncSessionLocal
from app.core.modules import module_registry
from app.core.nsp2 import create_package_streaming
from app.core.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packages", tags=["packages"])

PACKAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RESOURCE_TYPES = {"binary", "container", "css", "html", "image", "js", "json", "sealed", "text"}
# NSP2 (v2) packs everything except `sealed` resources.
# Binary media (mp4, webm, mp3, epub, cbz, …) are now native to the container.
PACKABLE_RESOURCE_TYPES = {"container", "css", "html", "image", "js", "json", "text", "binary"}
STANDALONE_RESOURCE_TYPES = {"sealed"}


class PackageResourceError(RuntimeError):
    pass


def _parse_range_header(range_header: str | None, content_length: int) -> tuple[int, int] | None:
    """Parse ``Range: bytes=...`` header.

    Returns ``(start, length)`` or ``None`` if the range is invalid.
    Only supports single-range requests like ``bytes=0-499`` or ``bytes=100-``.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None
    range_spec = range_header[len("bytes=") :]
    parts = range_spec.split("-", 1)
    try:
        if parts[0] and parts[1]:
            start, end = int(parts[0]), int(parts[1])
            length = min(end - start + 1, content_length - start)
            if length <= 0 or start >= content_length:
                return None
            return start, length
        elif parts[0]:
            start = int(parts[0])
            if start >= content_length:
                return None
            return start, content_length - start
        elif parts[1]:
            suffix = int(parts[1])
            if suffix <= 0 or suffix > content_length:
                return None
            start = content_length - suffix
            return start, suffix
    except (ValueError, TypeError):
        return None


def _build_etag(data_len: int, last_modified: str = "") -> str:
    """Build a weak ETag from file size and optional last-modified."""
    return f'W/"{hashlib.md5(f"{data_len}:{last_modified}".encode()).hexdigest()[:16]}"'


def _validate_local_url(url: str, field: str) -> None:
    parsed = urlsplit(url)
    if not url.startswith("/") or parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError(f"{field} must be a local absolute URL without a fragment: {url!r}")


def normalize_package_resources(resources: list[dict]) -> list[dict]:
    """Validate and de-duplicate resources while preserving manifest order."""
    normalized = []
    seen = {}
    for resource in resources:
        url = resource.get("url")
        resource_type = resource.get("type")
        if not isinstance(url, str) or not url:
            raise ValueError("Package resource URL must be a non-empty string")
        _validate_local_url(url, "Package resource URL")
        if resource_type not in RESOURCE_TYPES:
            raise ValueError(f"Unsupported package resource type: {resource_type!r}")
        size = resource.get("size")
        sha256 = resource.get("sha256")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise ValueError(f"Package resource size must be a non-negative integer: {url!r}")
        if sha256 is not None and (not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256)):
            raise ValueError(f"Package resource sha256 must be lowercase hexadecimal: {url!r}")
        if url in seen:
            if seen[url] != resource:
                raise ValueError(f"Conflicting package resource definitions: {url!r}")
            continue
        normalized_resource = {**resource, "url": url, "type": resource_type}
        seen[url] = normalized_resource
        normalized.append(normalized_resource)
    return normalized


def make_package_manifest(
    *,
    module_id: str,
    package_id: str,
    package_title: str,
    root_url: str,
    resources: list[dict],
) -> dict:
    """Build the versioned, module-agnostic offline package contract (NSP2)."""
    if not PACKAGE_ID_PATTERN.fullmatch(package_id):
        raise ValueError(f"Invalid package ID: {package_id!r}")
    _validate_local_url(root_url, "Package root URL")
    resources = normalize_package_resources(resources)
    record = next(
        (record for record in module_registry.active_records() if record.id == module_id and record.spec),
        None,
    )
    if not record or not record.spec or not record.spec.dashboard_url:
        raise RuntimeError(f"Active module {module_id!r} has no offline-capable dashboard")
    return {
        "schema_version": 2,
        "package": {
            "id": package_id,
            "title": package_title,
            "revision": 1,
            "root_url": root_url,
            "module": {
                "id": record.spec.id,
                "title": record.spec.title_en,
                "root_url": record.spec.dashboard_url,
            },
        },
        "root_url": root_url,
        "resources": resources,
    }


async def get_resources_for_package(pkg_id: str) -> list:
    """Resolve package_id to its full list of resource dictionaries by querying database."""
    resolver = module_registry.package_resolver(pkg_id)
    if resolver is None:
        raise HTTPException(status_code=400, detail=f"No active package provider for: {pkg_id}")

    async with AsyncSessionLocal() as db:
        try:
            resources = await resolver(pkg_id, db)
        except ValueError as exc:
            # Resolvers reject an id they do not own with ValueError, and their
            # messages ("Invalid Vault package ID") are written to be shown —
            # a bad id is the caller's mistake, not a server fault. An
            # HTTPException raised by a resolver is a deliberate 400/404 and
            # is not caught here; a ValueError from normalizing the *result*
            # still escapes as a 500, because that list is ours to build.
            logger.warning("Package resolver rejected '%s': %s", pkg_id, exc)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return normalize_package_resources(resources)


async def fetch_nsp_resource(
    res: dict, client: httpx.AsyncClient, headers: dict, cookies: dict, semaphore: asyncio.Semaphore
) -> dict:
    """Fetch single resource internally with semaphore locking."""
    url = res["url"]
    async with semaphore:
        try:
            response = await client.get(url, headers=headers, cookies=cookies)
        except Exception as exc:
            logger.error("NSP pack exception for %s: %s", url, exc)
            raise PackageResourceError(f"Could not fetch package resource {url}") from exc
        if response.status_code != 200:
            logger.warning("NSP pack failed for %s with code %s", url, response.status_code)
            raise PackageResourceError(f"Package resource {url} returned HTTP {response.status_code}")
        return {
            "url": url,
            "content": response.content,
            "mime": response.headers.get("content-type", "application/octet-stream"),
        }


async def build_nsp_to_file(
    resources: list,
    client: httpx.AsyncClient,
    headers: dict,
    cookies: dict,
    package_info: dict,
) -> tempfile.SpooledTemporaryFile[bytes]:
    """Build a complete NSP2 container, streaming directly to a tempfile on disk."""
    packable_resources = [res for res in resources if res.get("type") in PACKABLE_RESOURCE_TYPES]
    nsp_resources: list[dict] = []
    semaphore = asyncio.Semaphore(1)
    for resource_spec in packable_resources:
        resource = await fetch_nsp_resource(resource_spec, client, headers, cookies, semaphore)
        nsp_resources.append(
            {
                "url": resource["url"],
                "content": resource["content"],
                "mime": resource["mime"],
                "type": resource_spec.get("type", "text"),
            }
        )
    # Create_package_streaming writes directly to disk — no RAM bottleneck
    return create_package_streaming(
        resources=nsp_resources,
        package_info=package_info,
        sealed=False,
        padded=False,
    )


async def stream_nsp_file(
    package_file: tempfile.SpooledTemporaryFile[bytes],
    start: int = 0,
    length: int | None = None,
) -> AsyncGenerator[bytes, None]:
    """Yield NSP2 data from a seekable file, optionally a slice."""
    package_file.seek(start)
    end = length if length is not None else -1
    chunk_size = 64 * 1024
    while True:
        read_len = chunk_size if end < 0 else min(chunk_size, end - start)
        if read_len <= 0:
            break
        chunk = package_file.read(read_len)
        if not chunk:
            break
        yield chunk
        start += len(chunk)
    package_file.close()


async def generate_nsp(
    resources: list,
    client: httpx.AsyncClient,
    headers: dict,
    cookies: dict,
    package_info: dict,
) -> AsyncGenerator[bytes, None]:
    """Asynchronously stream NSP2 container payload chunk by chunk on the fly."""
    package_file = await build_nsp_to_file(resources, client, headers, cookies, package_info)
    async for chunk in stream_nsp_file(package_file):
        yield chunk


@router.get("/{package_id}/nsp", include_in_schema=False)
async def download_package_nsp(package_id: str, request: Request, user=Depends(get_current_user)):
    """Serve the complete NetSanctum Package container (.nsp) with Range support.

    Returns ``Accept-Ranges: bytes`` and supports ``Range: bytes=...`` → 206 Partial Content.
    Also returns ``Cache-Control: no-store`` and ``X-Content-Type-Options: nosniff``.
    """
    if not PACKAGE_ID_PATTERN.fullmatch(package_id):
        raise HTTPException(status_code=400, detail="Invalid package ID")
    # Resolve all resources to be packed
    resources = await get_resources_for_package(package_id)
    if not resources:
        raise HTTPException(status_code=404, detail="No package resources found to package.")

    # Resolve module_id from package_prefixes
    module_id = "unknown"
    for record in module_registry.active_records():
        if record.spec and record.spec.package_prefixes:
            if any(package_id.startswith(p) for p in record.spec.package_prefixes):
                module_id = record.spec.id
                break

    # Build package_info from manifest metadata
    manifest = make_package_manifest(
        module_id=module_id,
        package_id=package_id,
        package_title=package_id,
        root_url=f"/dashboard?package_id={package_id}",
        resources=resources,
    )

    # Prepare authorization forwarding
    headers: dict[str, str] = {}
    auth_header = request.headers.get("authorization")
    if auth_header:
        headers["authorization"] = auth_header

    cookies: dict[str, str] = {}
    access_token = request.cookies.get("access_token")
    if access_token:
        cookies["access_token"] = access_token

    # Build internal httpx client targeting our own FastAPI instance
    from app.main import app

    try:
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url=str(request.base_url))
    except AttributeError:
        # Fallback for older httpx versions
        client = httpx.AsyncClient(app=app, base_url=str(request.base_url))

    # Fetch before response headers are sent, so an incomplete package is never reported as successful.
    try:
        async with client:
            package_file = await build_nsp_to_file(resources, client, headers, cookies, manifest)
    except PackageResourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    package_file.seek(0, 2)  # EOF
    total_size = package_file.tell()
    filename = f"{package_id}.nsp"

    # Parse Range header
    range_header = request.headers.get("range")
    range_parsed = _parse_range_header(range_header, total_size)

    common_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }

    if range_parsed is not None:
        start, length = range_parsed
        etag = _build_etag(total_size)
        common_headers["ETag"] = etag
        common_headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{total_size}"
        common_headers["Content-Length"] = str(length)
        package_file.seek(start)
        return StreamingResponse(
            stream_nsp_file(package_file, start=0, length=length),
            media_type="application/octet-stream",
            headers=common_headers,
            status_code=206,
        )
    else:
        common_headers["Content-Length"] = str(total_size)
        etag = _build_etag(total_size)
        common_headers["ETag"] = etag
        package_file.seek(0)
        return StreamingResponse(
            stream_nsp_file(package_file, start=0, length=None),
            media_type="application/octet-stream",
            headers=common_headers,
        )


def make_hybrid_manifest(pkg_id: str, original_manifest: dict) -> dict:
    """Transform a standard manifest with a list of resources into a hybrid manifest that includes the .nsp container."""
    if not PACKAGE_ID_PATTERN.fullmatch(pkg_id):
        raise ValueError(f"Invalid package ID: {pkg_id!r}")
    original_resources = original_manifest.get("resources", [])

    # Binary and sealed files stay standalone resources; sealed ones additionally
    # never enter the plaintext container (see PACKABLE_RESOURCE_TYPES).
    standalone_resources = [res for res in original_resources if res.get("type") in STANDALONE_RESOURCE_TYPES]
    packable = [res for res in original_resources if res.get("type") in PACKABLE_RESOURCE_TYPES]
    if not packable:
        # Nothing to pack (e.g. a sealed-only package): a container with an empty
        # index would only force a useless download every refresh.
        new_manifest = original_manifest.copy()
        new_manifest["resources"] = standalone_resources
        return new_manifest

    # Add the NSP container resource
    container_url = f"/api/packages/{pkg_id}/nsp"
    standalone_resources.append({"url": container_url, "type": "container"})

    new_manifest = original_manifest.copy()
    new_manifest["resources"] = standalone_resources
    return new_manifest
