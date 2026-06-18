import json
import logging
import struct
from collections.abc import AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.database import AsyncSessionLocal
from app.core.security import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packages", tags=["packages"])

async def get_resources_for_package(pkg_id: str) -> list:
    """Resolve package_id to its full list of resource dictionaries by querying database."""
    async with AsyncSessionLocal() as db:
        if pkg_id.startswith("manga_") or pkg_id.startswith("novel_") or pkg_id.startswith("anime_"):
            from app.modules.alllib.router import get_media_sync_manifest
            media_id = int(pkg_id.split("_")[1])
            manifest = await get_media_sync_manifest(media_id, db=db, hybrid=False)
            return manifest.get("resources", [])

        elif pkg_id.startswith("song_"):
            from app.modules.music.router import get_song_sync_manifest
            song_id = int(pkg_id.split("_")[1])
            manifest = await get_song_sync_manifest(song_id, db=db, hybrid=False)
            return manifest.get("resources", [])

        elif pkg_id.startswith("playlist_"):
            from app.modules.music.router import get_playlist_sync_manifest
            playlist_id = int(pkg_id.split("_")[1])
            manifest = await get_playlist_sync_manifest(playlist_id, db=db, hybrid=False)
            return manifest.get("resources", [])

        elif pkg_id.startswith("video_playlist_"):
            from app.modules.video_archiver.router import (
                get_playlist_sync_manifest as get_video_playlist_sync_manifest,
            )
            playlist_id = int(pkg_id.split("_")[2])
            manifest = await get_video_playlist_sync_manifest(playlist_id, db=db, hybrid=False)
            return manifest.get("resources", [])

        elif pkg_id.startswith("video_"):
            from app.modules.video_archiver.router import get_video_sync_manifest
            video_id = pkg_id.split("_")[1]
            manifest = await get_video_sync_manifest(video_id, db=db, hybrid=False)
            return manifest.get("resources", [])

        else:
            raise HTTPException(status_code=400, detail=f"Unknown package prefix: {pkg_id}")


import asyncio


async def fetch_nsp_resource(
    res: dict, client: httpx.AsyncClient, headers: dict, cookies: dict, semaphore: asyncio.Semaphore
) -> dict | None:
    """Fetch single resource internally with semaphore locking."""
    url = res["url"]
    async with semaphore:
        try:
            response = await client.get(url, headers=headers, cookies=cookies)
            if response.status_code == 200:
                return {
                    "url": url,
                    "content": response.content,
                    "mime": response.headers.get("content-type", "application/octet-stream")
                }
            else:
                logger.warning(f"NSP pack failed for {url} with code {response.status_code}")
        except Exception as e:
            logger.error(f"NSP pack exception for {url}: {e}")
    return None


async def generate_nsp(resources: list, client: httpx.AsyncClient, headers: dict, cookies: dict) -> AsyncGenerator[bytes, None]:
    """Asynchronously stream NSP container payload chunk by chunk on the fly."""
    # Exclude large binary streams (videos, audio, epub exports) from being packed inside NSP
    packable_resources = [res for res in resources if res.get("type") != "binary"]

    # Use a Semaphore to prevent excessive resource allocation/concurrency spikes (max 20 parallel requests)
    semaphore = asyncio.Semaphore(20)
    tasks = [
        fetch_nsp_resource(res, client, headers, cookies, semaphore)
        for res in packable_resources
    ]

    index = {}
    offset = 0

    # Run requests and yield content chunks as soon as they complete
    for future in asyncio.as_completed(tasks):
        res_data = await future
        if not res_data:
            continue
        content = res_data["content"]
        length = len(content)

        index[res_data["url"]] = {
            "offset": offset,
            "length": length,
            "mime": res_data["mime"]
        }

        yield content
        offset += length

    # Write index dictionary as JSON
    index_bytes = json.dumps(index).encode("utf-8")
    yield index_bytes

    # Write Footer: Offset of index (8 bytes uint64) + Magic bytes 'NSPK' (4 bytes)
    footer = struct.pack(">Q4s", offset, b"NSPK")
    yield footer


@router.get("/{package_id}/nsp", include_in_schema=False)
async def download_package_nsp(
    package_id: str,
    request: Request,
    user=Depends(get_current_user)
):
    """Serve the complete NetSanctum Package container (.nsp) on-the-fly for the requested package_id."""
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

    # Return streamed response with clean cleanup of the client
    async def nsp_stream():
        async with client:
            async for chunk in generate_nsp(resources, client, headers, cookies):
                yield chunk

    filename = f"{package_id}.nsp"
    return StreamingResponse(
        nsp_stream(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


def make_hybrid_manifest(pkg_id: str, original_manifest: dict) -> dict:
    """Transform a standard manifest with a list of resources into a hybrid manifest that includes the .nsp container."""
    original_resources = original_manifest.get("resources", [])

    # Keep only binary files as standalone resources
    standalone_resources = [res for res in original_resources if res.get("type") == "binary"]

    # Add the NSP container resource
    container_url = f"/api/packages/{pkg_id}/nsp"
    standalone_resources.append({"url": container_url, "type": "container"})

    new_manifest = original_manifest.copy()
    new_manifest["resources"] = standalone_resources
    return new_manifest
