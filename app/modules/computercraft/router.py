import subprocess
from pathlib import Path
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.library_viewer_v1 import CONTRACT_ID, LibraryResult
from app.core.database import get_db
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationResource,
    IntegrationUnavailableError,
)
from app.core.modules import module_registry
from app.core.security import get_current_user
from app.core.storage import get_storage
from app.modules.computercraft.rendering import (
    CC_PALETTE,
    FRAME_MEDIA_TYPES,
    extract_media_frame,
    has_audio_stream,
    materialize_media,
    probe_media_duration,
    validate_frame_dimensions,
)
from app.modules.computercraft.streaming import build_audio_command, stop_process

router = APIRouter()


def _integration_id(module_id: str) -> str:
    provider = next(
        (
            integration.id
            for record, integration in module_registry.integration_providers(CONTRACT_ID)
            if record.id == module_id
        ),
        None,
    )
    if not provider:
        raise HTTPException(status_code=404, detail="ComputerCraft module provider was not found")
    return provider


async def _invoke(
    module_id: str,
    payload: dict[str, Any],
    db: AsyncSession,
    user,
) -> dict[str, Any]:
    integration_id = _integration_id(module_id)
    try:
        result = await module_registry.invoke_integration(
            integration_id,
            payload,
            IntegrationContext(session=db, user=user, registry=module_registry, consumer_id="computercraft"),
        )
        validated = LibraryResult.model_validate(result)
        if validated.module_id != module_id:
            raise HTTPException(status_code=422, detail="Integration returned the wrong module ID")
        return validated.model_dump(mode="json")
    except IntegrationUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntegrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    except IntegrationRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _resource(
    module_id: str,
    item_id: str,
    child_id: str | None,
    page: int | None,
    db: AsyncSession,
    user,
) -> IntegrationResource:
    try:
        resource = await module_registry.resolve_integration_resource(
            _integration_id(module_id),
            {"item_id": item_id, "child_id": child_id, "page": page},
            IntegrationContext(session=db, user=user, registry=module_registry, consumer_id="computercraft"),
        )
    except IntegrationUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntegrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    except IntegrationRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if resource.storage_path:
        namespace = resource.storage_path.partition("/")[0]
        if module_registry.storage_owner(namespace) != module_id:
            raise HTTPException(status_code=422, detail="Provider returned a foreign storage resource")
    return resource


@router.get("/computercraft/client.lua", include_in_schema=False)
async def get_client():
    return Response(
        content=Path(__file__).with_name("netsanctum_os.lua").read_text(),
        media_type="text/x-lua; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/api/computercraft/system")
async def get_system(db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    modules = []
    for record, _integration in module_registry.integration_providers(CONTRACT_ID):
        module_id = record.id
        result = await _invoke(module_id, {"operation": "catalog", "limit": 1}, db, user)
        modules.append(
            {
                "id": module_id,
                "title": result["title"],
                "order": result["order"],
            }
        )
    return {"name": "NetSanctumOS", "version": "0.1.0", "modules": sorted(modules, key=lambda x: x["order"])}


@router.get("/api/computercraft/modules/{module_id}/items")
async def list_items(
    module_id: str,
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    return await _invoke(
        module_id,
        {"operation": "catalog", "limit": limit, "offset": offset},
        db,
        user,
    )


@router.get("/api/computercraft/modules/{module_id}/items/{item_id}")
async def get_item(
    module_id: str,
    item_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    return await _invoke(module_id, {"operation": "detail", "item_id": item_id}, db, user)


@router.get("/api/computercraft/modules/{module_id}/items/{item_id}/text")
async def get_text(
    module_id: str,
    item_id: str,
    child_id: str,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    resource = await _resource(module_id, item_id, child_id, None, db, user)
    if resource.kind != "text":
        raise HTTPException(status_code=422, detail="Resource is not readable text")
    return {"title": resource.title, "text": resource.text or ""}


@router.get("/api/computercraft/modules/{module_id}/items/{item_id}/frame")
async def get_frame(
    module_id: str,
    item_id: str,
    response: Response,
    child_id: str | None = None,
    page: int | None = Query(default=None, ge=0),
    time: float = Query(0, ge=0),
    width: int = Query(32, ge=1, le=2048),
    height: int = Query(18, ge=1, le=2048),
    frame_format: Literal["cc-palette", "nfp", "png", "jpeg", "webp"] = Query("cc-palette", alias="format"),
    fit: Literal["contain", "cover", "stretch"] = "contain",
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    resource = await _resource(module_id, item_id, child_id, page, db, user)
    if resource.kind not in {"video", "image"} or not resource.storage_path:
        raise HTTPException(status_code=422, detail="Resource has no visual media")
    duration = max(0, resource.duration)
    storage = get_storage()
    path = resource.storage_path
    if not await anyio.to_thread.run_sync(storage.file_exists, path):
        raise HTTPException(status_code=404, detail="Media file is missing from storage")
    try:
        if not duration and resource.kind == "video":
            duration = await anyio.to_thread.run_sync(probe_media_duration, storage, path)
        timestamp = min(time, max(0, duration - 0.05)) if duration else time
        validate_frame_dimensions(width, height, frame_format)
        frame = await anyio.to_thread.run_sync(
            extract_media_frame, storage, path, timestamp, width, height, frame_format, fit
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="Media frame extraction timed out") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    headers = {
        "Cache-Control": "no-store",
        "X-Frame-Format": frame_format,
        "X-Frame-Size": f"{width}x{height}",
        "X-Frame-Time": f"{timestamp:.3f}",
    }
    if frame_format == "cc-palette":
        response.headers.update(headers)
        return {
            "format": frame_format,
            "fit": fit,
            "width": width,
            "height": height,
            "time": round(timestamp, 3),
            "duration": duration,
            "palette": {code: f"#{red:02x}{green:02x}{blue:02x}" for code, red, green, blue in CC_PALETTE},
            "rows": frame,
        }
    if frame_format == "nfp":
        return Response(
            content="\n".join(frame) + "\n", media_type=FRAME_MEDIA_TYPES[frame_format], headers=headers
        )
    return Response(content=frame, media_type=FRAME_MEDIA_TYPES[frame_format], headers=headers)


@router.get("/api/computercraft/modules/{module_id}/items/{item_id}/audio")
async def get_audio(
    module_id: str,
    item_id: str,
    child_id: str | None = None,
    time: float = Query(0, ge=0),
    audio_format: Literal["mp3", "dfpwm"] = Query("dfpwm", alias="format"),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    resource = await _resource(module_id, item_id, child_id, None, db, user)
    if resource.kind not in {"audio", "video"} or not resource.storage_path:
        raise HTTPException(status_code=422, detail="Resource has no audio media")
    duration = max(0, resource.duration)
    storage = get_storage()
    path = resource.storage_path
    if not await anyio.to_thread.run_sync(storage.file_exists, path):
        raise HTTPException(status_code=404, detail="Audio file is missing from storage")
    try:
        media_path = await anyio.to_thread.run_sync(materialize_media, storage, path)
        if not duration:
            duration = await anyio.to_thread.run_sync(probe_media_duration, storage, path)
        if not await anyio.to_thread.run_sync(has_audio_stream, storage, path):
            raise HTTPException(status_code=422, detail="Media has no audio stream")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    timestamp = min(time, max(0, duration - 0.05)) if duration else time
    command = build_audio_command(str(media_path), timestamp, audio_format, seekable=True)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    process_stdout = process.stdout

    async def iter_audio():
        try:
            while chunk := await anyio.to_thread.run_sync(process_stdout.read, 16384):
                yield chunk
        finally:
            await anyio.to_thread.run_sync(stop_process, process)

    return StreamingResponse(
        iter_audio(),
        media_type="audio/x-dfpwm" if audio_format == "dfpwm" else "audio/mpeg",
        headers={"Cache-Control": "no-store", "X-Audio-Time": f"{timestamp:.3f}"},
    )
