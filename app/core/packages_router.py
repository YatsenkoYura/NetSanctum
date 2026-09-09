import asyncio
import hashlib
import json
import logging
import re
import struct
import tempfile
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.database import AsyncSessionLocal
from app.core.modules import module_registry
from app.core.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packages", tags=["packages"])

PACKAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
RESOURCE_TYPES = {"binary", "container", "css", "html", "image", "js", "json", "text"}


class PackageResourceError(RuntimeError):
    pass


def _validate_local_url(url: str, field: str) -> None:
    parsed = urlsplit(url)
    if not url.startswith("/") or parsed.scheme or parsed.netloc or parsed.fragment:
        raise ValueError(f"{field} must be a local absolute URL without a fragment: {url!r}")


def normalize_package_resources(resources: list[dict]) -> list[dict]:
    """Validate and de-duplicate resources while preserving manifest order."""
    normalized = []
    seen = set()
    for resource in resources:
        url = resource.get("url")
        resource_type = resource.get("type")
        if not isinstance(url, str) or not url:
            raise ValueError("Package resource URL must be a non-empty string")
        _validate_local_url(url, "Package resource URL")
        if resource_type not in RESOURCE_TYPES:
            raise ValueError(f"Unsupported package resource type: {resource_type!r}")
        if url in seen:
            continue
        seen.add(url)
        normalized.append({**resource, "url": url, "type": resource_type})
    return normalized


def make_package_manifest(
    *,
    module_id: str,
    package_id: str,
    package_title: str,
    root_url: str,
    resources: list[dict],
) -> dict:
    """Build the versioned, module-agnostic offline package contract."""
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
        "schema_version": 1,
        "package_id": package_id,
        "package_title": package_title,
        "package_name": package_title,
        "title": package_title,
        "name": package_title,
        "module": {
            "id": record.spec.id,
            "title": record.spec.title_en,
            "title_en": record.spec.title_en,
            "title_ru": record.spec.title_ru,
            "root_url": record.spec.dashboard_url,
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
        return normalize_package_resources(await resolver(pkg_id, db))


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


async def build_nsp_file(
    resources: list, client: httpx.AsyncClient, headers: dict, cookies: dict
) -> tempfile.SpooledTemporaryFile[bytes]:
    """Build a complete container in bounded temporary storage before it is served."""
    packable_resources = [res for res in resources if res.get("type") != "binary"]
    package_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    index = {}
    offset = 0
    semaphore = asyncio.Semaphore(1)
    try:
        for resource_spec in packable_resources:
            resource = await fetch_nsp_resource(resource_spec, client, headers, cookies, semaphore)
            content = resource["content"]
            length = len(content)
            index[resource["url"]] = {
                "offset": offset,
                "length": length,
                "mime": resource["mime"],
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            package_file.write(content)
            offset += length

        package_file.write(json.dumps(index, separators=(",", ":")).encode("utf-8"))
        package_file.write(struct.pack(">Q4s", offset, b"NSPK"))
        package_file.seek(0)
        return package_file
    except Exception:
        package_file.close()
        raise


async def stream_nsp_file(
    package_file: tempfile.SpooledTemporaryFile[bytes],
) -> AsyncGenerator[bytes, None]:
    try:
        while chunk := package_file.read(64 * 1024):
            yield chunk
    finally:
        package_file.close()


async def generate_nsp(
    resources: list, client: httpx.AsyncClient, headers: dict, cookies: dict
) -> AsyncGenerator[bytes, None]:
    """Asynchronously stream NSP container payload chunk by chunk on the fly."""
    package_file = await build_nsp_file(resources, client, headers, cookies)
    async for chunk in stream_nsp_file(package_file):
        yield chunk


@router.get("/{package_id}/nsp", include_in_schema=False)
async def download_package_nsp(package_id: str, request: Request, user=Depends(get_current_user)):
    """Serve the complete NetSanctum Package container (.nsp) on-the-fly for the requested package_id."""
    if not PACKAGE_ID_PATTERN.fullmatch(package_id):
        raise HTTPException(status_code=400, detail="Invalid package ID")
    # Resolve all resources to be packed
    resources = await get_resources_for_package(package_id)
    if not resources:
        raise HTTPException(status_code=404, detail="No package resources found to package.")

    # Prepare authorization forwarding
    headers = {}
    auth_header = request.headers.get("authorization")
    if auth_header:
        headers["authorization"] = auth_header

    cookies = {}
    access_token = request.cookies.get("access_token")
    if access_token:
        cookies["access_token"] = access_token

    # Build internal httpx client targeting our own FastAPI instance
    from app.main import app

    try:
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://netsanctum.internal")
    except AttributeError:
        # Fallback for older httpx versions
        client = httpx.AsyncClient(app=app, base_url="http://netsanctum.internal")

    # Fetch before response headers are sent, so an incomplete package is never reported as successful.
    try:
        async with client:
            package_file = await build_nsp_file(resources, client, headers, cookies)
    except PackageResourceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    package_size = package_file.seek(0, 2)
    package_file.seek(0)
    filename = f"{package_id}.nsp"
    return StreamingResponse(
        stream_nsp_file(package_file),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Content-Length": str(package_size),
        },
    )


def make_hybrid_manifest(pkg_id: str, original_manifest: dict) -> dict:
    """Transform a standard manifest with a list of resources into a hybrid manifest that includes the .nsp container."""
    if not PACKAGE_ID_PATTERN.fullmatch(pkg_id):
        raise ValueError(f"Invalid package ID: {pkg_id!r}")
    original_resources = original_manifest.get("resources", [])

    # Keep only binary files as standalone resources
    standalone_resources = [res for res in original_resources if res.get("type") == "binary"]

    # Add the NSP container resource
    container_url = f"/api/packages/{pkg_id}/nsp"
    standalone_resources.append({"url": container_url, "type": "container"})

    new_manifest = original_manifest.copy()
    new_manifest["resources"] = standalone_resources
    return new_manifest
