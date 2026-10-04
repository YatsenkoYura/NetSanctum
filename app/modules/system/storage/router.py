"""
Storage module router.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.modules import module_registry
from app.core.security import OwnerUser, get_current_user
from app.core.storage import get_storage
from app.core.templates import templates
from app.modules.system.storage.browse import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ModuleOwnedPathError,
    StoragePathError,
    UnsupportedOnBackendError,
    breadcrumbs,
    guess_media_type,
    is_module_owned,
    is_remote,
    join_folder,
    list_folder,
    normalize_folder,
    parent_of,
    read_object,
    resolve_local,
    safe_segment,
    storage_root,
)

logger = logging.getLogger(__name__)

settings = get_settings()
redis_client = aioredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

router = APIRouter(prefix="/storage", tags=["Storage"])
STORAGE_PACKAGE_ID = "storage_manager"
# A single upload is bounded here rather than by the request body limit, so an
# accidental 4 GB video cannot be streamed into the storage tree unnoticed.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024 * 1024


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{round(size_bytes / 1024, 1)} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{round(size_bytes / (1024 * 1024), 1)} MB"
    else:
        return f"{round(size_bytes / (1024 * 1024 * 1024), 2)} GB"


def _get_storage_stats() -> dict:
    storage_root = Path(settings.LOCAL_STORAGE_ROOT).resolve()

    # 1. Total disk usage (only makes sense for local filesystem)
    if settings.STORAGE_BACKEND != "s3" and storage_root.exists():
        try:
            total, used, free = shutil.disk_usage(storage_root)
        except Exception:
            total, used, free = 1, 0, 1
    else:
        total, used, free = 0, 0, 0

    module_sizes = {}
    file_counts = {}
    large_files = []

    if settings.STORAGE_BACKEND == "s3":
        try:
            storage = get_storage()
            client = storage._client
            bucket = storage._bucket
            paginator = client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=bucket)

            for page in pages:
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    size = obj["Size"]

                    parts = key.split("/")
                    module_name = parts[0] if parts else "other"

                    if module_name not in module_sizes:
                        module_sizes[module_name] = 0
                        file_counts[module_name] = 0
                    module_sizes[module_name] += size
                    file_counts[module_name] += 1

                    large_files.append(
                        {
                            "path": key,
                            "name": parts[-1] if parts else key,
                            "size": size,
                            "module": module_name,
                        }
                    )
        except Exception as e:
            logger.error(f"Failed to list S3 objects for storage stats: {e}")
    else:
        if storage_root.exists():
            for root, _, files in os.walk(storage_root):
                for file in files:
                    full_path = Path(root) / file
                    try:
                        size = full_path.stat().st_size
                    except Exception:
                        continue

                    try:
                        rel_path = full_path.relative_to(storage_root)
                    except ValueError:
                        continue

                    parts = rel_path.parts
                    module_name = parts[0] if parts else "other"

                    if module_name not in module_sizes:
                        module_sizes[module_name] = 0
                        file_counts[module_name] = 0
                    module_sizes[module_name] += size
                    file_counts[module_name] += 1

                    large_files.append(
                        {
                            "path": str(rel_path),
                            "name": file,
                            "size": size,
                            "module": module_name,
                        }
                    )

    large_files.sort(key=lambda x: x["size"], reverse=True)
    large_files = large_files[:50]
    total_used = sum(module_sizes.values())

    modules_list = []
    for name, size in module_sizes.items():
        modules_list.append(
            {
                "name": name,
                "size": size,
                "file_count": file_counts[name],
                "size_human": format_size(size),
            }
        )
    modules_list.sort(key=lambda x: x["name"])

    return {
        "total": total,
        "used": total_used if settings.STORAGE_BACKEND == "s3" else used,
        "free": free,
        "used_percent": round((used / total) * 100, 1) if total else (100.0 if total_used else 0.0),
        "total_human": format_size(total) if total else "Unlimited (S3)",
        "used_human": format_size(total_used if settings.STORAGE_BACKEND == "s3" else used),
        "free_human": format_size(free) if free else "N/A",
        "is_s3": settings.STORAGE_BACKEND == "s3",
        "bucket_name": settings.S3_BUCKET_NAME if settings.STORAGE_BACKEND == "s3" else None,
        "modules": modules_list,
        "large_files": [{**f, "size_human": format_size(f["size"])} for f in large_files],
    }


async def _get_user_from_cookie(request: Request) -> OwnerUser | None:
    session_id = request.cookies.get("access_token")
    if not session_id:
        return None
    if await redis_client.get(f"session:{session_id}") == "1":
        return OwnerUser()
    return None


async def cleanup_database_for_file(db: AsyncSession, path: str):
    module_id = path.split("/", 1)[0]
    hook = module_registry.file_cleanup_hook(module_id)
    if hook:
        try:
            await hook(db, path)
        except Exception as e:
            logger.error(f"Error executing file deletion hook: {e}")


async def cleanup_database_for_module(db: AsyncSession, module: str):
    hook = module_registry.module_cleanup_hook(module)
    if hook:
        try:
            await hook(db)
        except Exception as e:
            logger.error(f"Error executing module cleanup hook for {module}: {e}")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def storage_dashboard(
    request: Request,
    package_id: str | None = Query(None),
    path: str = Query(""),
    user=Depends(get_current_user),
):
    if package_id and package_id != STORAGE_PACKAGE_ID:
        raise HTTPException(status_code=400, detail="Invalid Storage package ID")
    lang = request.cookies.get("lang") or "en"
    stats = await asyncio.to_thread(_get_storage_stats)
    folder = ""
    listing = None
    error = None
    if not package_id:
        try:
            listing = await asyncio.to_thread(list_folder, path)
            folder = listing.path
        except StoragePathError as exc:
            error = str(exc)
    return templates.TemplateResponse(
        request,
        "storage_dashboard.html",
        {
            "user": user,
            "lang": lang,
            "stats": stats,
            "package_mode": bool(package_id),
            "is_readonly": bool(package_id),
            "folder": folder,
            "breadcrumbs": breadcrumbs(folder),
            "parent_path": parent_of(folder),
            "entries": [entry.as_dict(format_size=format_size) for entry in listing.entries]
            if listing
            else [],
            "listing_total": listing.total if listing else 0,
            "remote_backend": is_remote(),
            "browse_error": error,
        },
    )


# ── FOLDER MANAGER ──────────────────────────────────────────────────────
# Read one folder. Never recursive: a whole tree would mean walking every byte
# of every module on each click.


@router.get("/api/folder")
async def api_list_folder(
    path: str = Query(""),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
):
    try:
        listing = await asyncio.to_thread(list_folder, path, limit=limit, offset=offset)
    except ModuleOwnedPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedOnBackendError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Folder not found") from exc
    return {
        "path": listing.path,
        "parent": parent_of(listing.path),
        "backend": listing.backend,
        "total": listing.total,
        "truncated": listing.truncated,
        "entries": [entry.as_dict(format_size=format_size) for entry in listing.entries],
    }


@router.get("/api/download")
async def api_download(
    path: str = Query(...),
    user=Depends(get_current_user),
):
    """Download one file. Encrypted objects are decrypted on the way out."""
    try:
        stream, size, media_type, name = await asyncio.to_thread(read_object, path)
    except ModuleOwnedPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Unreadable file: {exc}") from exc

    async def body():
        # A seekable envelope yields chunk by chunk; a plain stream is iterated
        # in blocks. Neither holds the whole file in memory.
        iterator = stream
        if hasattr(stream, "read"):

            def _read():
                while True:
                    block = stream.read(1024 * 1024)
                    if not block:
                        return
                    yield block

            iterator = _read()
        try:
            for block in iterator:
                yield block
        finally:
            closer = getattr(stream, "close", None)
            if callable(closer):
                closer()

    headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
    if size:
        headers["Content-Length"] = str(size)
    return StreamingResponse(
        body(),
        media_type=media_type,
        headers={**headers, "Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/api/mkdir")
async def api_mkdir(
    payload: dict,
    user=Depends(get_current_user),
):
    """Create a folder inside another folder."""
    if is_remote():
        raise HTTPException(status_code=422, detail="Folders are a local-backend feature")
    try:
        folder = normalize_folder(payload.get("path"))
        name = safe_segment(str(payload.get("name") or ""), fallback="")
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not name or name == "file":
        raise HTTPException(status_code=400, detail="A folder name is required")
    target = resolve_local(join_folder(folder, name))
    if target.exists():
        raise HTTPException(status_code=409, detail="A folder with that name already exists")

    def _mkdir() -> None:
        target.mkdir(parents=True, exist_ok=False)

    try:
        await asyncio.to_thread(_mkdir)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="A folder with that name already exists") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not create the folder: {exc}") from exc
    return {"status": "ok", "path": f"{folder}/{name}" if folder else name}


@router.post("/api/upload")
async def api_upload(
    file: UploadFile = File(...),
    path: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Store an uploaded file in a folder of the storage tree."""
    try:
        folder = normalize_folder(path)
        name = safe_segment(file.filename or "", fallback="upload.bin")
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if is_remote():
        raise HTTPException(status_code=422, detail="Uploads require the local backend")
    target_path = join_folder(folder, name)
    target = resolve_local(target_path)
    if not target.parent.exists():
        raise HTTPException(status_code=404, detail="Folder not found")
    if await asyncio.to_thread(get_storage().file_exists, target_path):
        raise HTTPException(status_code=409, detail="A file with that name already exists")

    def _write() -> int:
        written = 0
        with target.open("wb") as sink:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    sink.close()
                    target.unlink(missing_ok=True)
                    raise ValueError("The file is larger than the upload limit")
                sink.write(chunk)
        return written

    try:
        written = await asyncio.to_thread(_write)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not store the file: {exc}") from exc
    finally:
        await file.close()
    return {"status": "ok", "path": target_path, "size": written, "media_type": guess_media_type(name)}


@router.post("/api/rename")
async def api_rename(
    payload: dict,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Rename a file or a folder in place."""
    if is_remote():
        raise HTTPException(status_code=422, detail="Renaming requires the local backend")
    if is_module_owned(str(payload.get("path") or "")):
        # Renaming a Vault's file would break the row that points at it, and the
        # browser has no way to update that row. Same rule as the download.
        raise HTTPException(status_code=403, detail="This folder belongs to a module and is managed by it")
    try:
        source = normalize_folder(payload.get("path"))
        name = safe_segment(str(payload.get("name") or ""), fallback="")
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not source:
        raise HTTPException(status_code=400, detail="The storage root cannot be renamed")
    if not name or name == "file":
        raise HTTPException(status_code=400, detail="A new name is required")
    folder = parent_of(source)
    destination_path = join_folder(folder, name)
    source_path = resolve_local(source)
    destination = resolve_local(destination_path)
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if destination.exists():
        raise HTTPException(status_code=409, detail="A folder or file with that name already exists")

    def _rename() -> None:
        source_path.rename(destination)

    try:
        await asyncio.to_thread(_rename)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not rename: {exc}") from exc

    if source_path.is_dir():
        # A folder move invalidates every module path underneath it.
        top_segment = source.split("/", 1)[0]
        await cleanup_database_for_module(db, top_segment)
        await db.commit()
        return {"status": "ok", "path": destination_path, "is_dir": True}
    await cleanup_database_for_file(db, destination_path)
    await db.commit()
    return {"status": "ok", "path": destination_path, "is_dir": False}


@router.delete("/api/entry")
async def api_delete_entry(
    path: str = Query(...),
    recursive: bool = Query(True),
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Delete one file, or a folder and everything in it."""
    if is_module_owned(path):
        # Deleting from here would leave the owning module's rows pointing at
        # bytes that are gone, with no cleanup hook to hear about it.
        raise HTTPException(status_code=403, detail="This folder belongs to a module and is managed by it")
    try:
        target_logical = normalize_folder(path)
    except StoragePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not target_logical:
        raise HTTPException(status_code=400, detail="The storage root cannot be deleted")
    if is_remote():
        backend = get_storage()
        if not await asyncio.to_thread(backend.file_exists, target_logical):
            raise HTTPException(status_code=404, detail="Not found")
        await asyncio.to_thread(backend.delete_file, target_logical)
        await cleanup_database_for_file(db, target_logical)
        await db.commit()
        return {"status": "ok", "path": target_logical}

    target = resolve_local(target_logical)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if target.is_dir():
        removed_files = await asyncio.to_thread(_delete_folder_files, target)

        def _rmdir() -> None:
            shutil.rmtree(target)

        try:
            await asyncio.to_thread(_rmdir)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Could not delete the folder: {exc}") from exc
        for logical in removed_files:
            await cleanup_database_for_file(db, logical)
        top_segment = target_logical.split("/", 1)[0]
        await cleanup_database_for_module(db, top_segment)
        await db.commit()
        return {"status": "ok", "path": target_logical, "files_removed": len(removed_files)}

    await asyncio.to_thread(get_storage().delete_file, target_logical)
    await cleanup_database_for_file(db, target_logical)
    await db.commit()
    return {"status": "ok", "path": target_logical, "files_removed": 1}


def _delete_folder_files(target: Path) -> list[str]:
    """Logical paths of every file under `target`, before the tree is removed."""
    root = storage_root()
    collected: list[str] = []
    for current, _dirs, files in os.walk(target):
        for name in files:
            full = Path(current) / name
            try:
                collected.append(str(full.relative_to(root)))
            except ValueError:
                continue
    return collected


@router.post("/api/recalculate", response_class=HTMLResponse, include_in_schema=False)
async def api_recalculate(request: Request, user=Depends(get_current_user)):
    stats = await asyncio.to_thread(_get_storage_stats)
    lang = request.cookies.get("lang") or "en"
    return templates.TemplateResponse(
        request, "storage_dashboard.html", {"user": user, "lang": lang, "stats": stats, "only_stats": True}
    )


@router.delete("/api/file", include_in_schema=False)
async def delete_file(
    request: Request, path: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    storage = get_storage()
    if await asyncio.to_thread(storage.file_exists, path):
        await asyncio.to_thread(storage.delete_file, path)
        await cleanup_database_for_file(db, path)
        await db.commit()

    stats = await asyncio.to_thread(_get_storage_stats)
    lang = request.cookies.get("lang") or "en"
    return templates.TemplateResponse(
        request, "storage_dashboard.html", {"user": user, "lang": lang, "stats": stats, "only_stats": True}
    )


@router.post("/api/clean-module", include_in_schema=False)
async def clean_module(
    request: Request, module: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    if module != "other" and module_registry.storage_owner(module) is None:
        raise HTTPException(status_code=400, detail="Invalid module")

    def do_clean():
        storage = get_storage()
        storage_root = Path(settings.LOCAL_STORAGE_ROOT).resolve()

        if settings.STORAGE_BACKEND == "s3":
            try:
                client = storage._client
                bucket = storage._bucket
                paginator = client.get_paginator("list_objects_v2")
                pages = paginator.paginate(Bucket=bucket, Prefix=f"{module}/")

                for page in pages:
                    for obj in page.get("Contents", []):
                        storage.delete_file(obj["Key"])
            except Exception as e:
                logger.error(f"Failed to clear S3 module folder: {e}")
        else:
            module_path = storage_root / module
            if module_path.exists() and module_path.is_dir():
                shutil.rmtree(module_path)
                module_path.mkdir(parents=True, exist_ok=True)

    await asyncio.to_thread(do_clean)

    await cleanup_database_for_module(db, module)
    await db.commit()

    stats = await asyncio.to_thread(_get_storage_stats)
    lang = request.cookies.get("lang") or "en"
    return templates.TemplateResponse(
        request, "storage_dashboard.html", {"user": user, "lang": lang, "stats": stats, "only_stats": True}
    )


@router.get("/api/sync-manifest", include_in_schema=False)
async def get_storage_sync_manifest(
    user=Depends(get_current_user),
    hybrid: bool = True,
):
    """API: Sync manifest for offline access to storage panel."""
    from app.core.packages_router import make_hybrid_manifest, make_package_manifest

    manifest = make_package_manifest(
        module_id="storage",
        package_id=STORAGE_PACKAGE_ID,
        package_title="Storage Manager",
        root_url=f"/storage/dashboard?package_id={STORAGE_PACKAGE_ID}",
        resources=[
            {"url": "/static/tailwind.css", "type": "css"},
            {"url": "/static/htmx.min.js", "type": "js"},
            {"url": f"/storage/dashboard?package_id={STORAGE_PACKAGE_ID}", "type": "html"},
        ],
    )
    return make_hybrid_manifest(STORAGE_PACKAGE_ID, manifest) if hybrid else manifest
