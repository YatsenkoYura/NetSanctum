"""
Storage module router.
"""

import os
import shutil
import logging
import mimetypes
from pathlib import Path

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.core.security import OwnerUser, get_current_user
from app.core.storage import get_storage, LocalStorage, S3Storage
from app.core.templates import templates

logger = logging.getLogger(__name__)

settings = get_settings()
redis_client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

router = APIRouter(prefix="/storage", tags=["Storage"])


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
                        {"path": str(rel_path), "name": file, "size": size, "module": module_name}
                    )

    large_files.sort(key=lambda x: x["size"], reverse=True)
    large_files = large_files[:50]
    total_used = sum(module_sizes.values())

    modules_list = []
    for name, size in module_sizes.items():
        modules_list.append(
            {"name": name, "size": size, "file_count": file_counts[name], "size_human": format_size(size)}
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
    if redis_client.get(f"session:{session_id}") == "1":
        return OwnerUser()
    return None


# ── Dynamic DB Cleanups Callback Registration ───────────
FILE_DELETION_HOOKS = []
MODULE_CLEANUP_HOOKS = {}


def register_file_deletion_hook(hook):
    """
    Register a callback to clean up database references when a storage file is deleted.
    Signature: async def hook(db: AsyncSession, path: str) -> None
    """
    FILE_DELETION_HOOKS.append(hook)


def register_module_cleanup_hook(module_name: str, hook):
    """
    Register a callback to clean up database records when a whole module folder is wiped.
    Signature: async def hook(db: AsyncSession) -> None
    """
    MODULE_CLEANUP_HOOKS[module_name] = hook


async def cleanup_database_for_file(db: AsyncSession, path: str):
    for hook in FILE_DELETION_HOOKS:
        try:
            await hook(db, path)
        except Exception as e:
            logger.error(f"Error executing file deletion hook: {e}")


async def cleanup_database_for_module(db: AsyncSession, module: str):
    hook = MODULE_CLEANUP_HOOKS.get(module)
    if hook:
        try:
            await hook(db)
        except Exception as e:
            logger.error(f"Error executing module cleanup hook for {module}: {e}")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def storage_dashboard(request: Request, user=Depends(get_current_user)):
    lang = request.cookies.get("lang") or "en"
    stats = _get_storage_stats()
    return templates.TemplateResponse(
        request, "storage_dashboard.html", {"user": user, "lang": lang, "stats": stats}
    )


@router.post("/api/recalculate", response_class=HTMLResponse, include_in_schema=False)
async def api_recalculate(request: Request, user=Depends(get_current_user)):
    stats = _get_storage_stats()
    lang = request.cookies.get("lang") or "en"
    return templates.TemplateResponse(
        request, "storage_dashboard.html", {"user": user, "lang": lang, "stats": stats, "only_stats": True}
    )


@router.delete("/api/file", include_in_schema=False)
async def delete_file(
    request: Request, path: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    storage = get_storage()
    if storage.file_exists(path):
        storage.delete_file(path)
        await cleanup_database_for_file(db, path)
        await db.commit()

    stats = _get_storage_stats()
    lang = request.cookies.get("lang") or "en"
    return templates.TemplateResponse(
        request, "storage_dashboard.html", {"user": user, "lang": lang, "stats": stats, "only_stats": True}
    )


@router.post("/api/clean-module", include_in_schema=False)
async def clean_module(
    request: Request, module: str, db: AsyncSession = Depends(get_db), user=Depends(get_current_user)
):
    if module not in ("ranobelib", "music", "video_archiver", "other"):
        raise HTTPException(status_code=400, detail="Invalid module")

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

    await cleanup_database_for_module(db, module)
    await db.commit()

    stats = _get_storage_stats()
    lang = request.cookies.get("lang") or "en"
    return templates.TemplateResponse(
        request, "storage_dashboard.html", {"user": user, "lang": lang, "stats": stats, "only_stats": True}
    )


@router.get("/api/sync-manifest", include_in_schema=False)
async def get_storage_sync_manifest(user=Depends(get_current_user)):
    """API: Sync manifest for offline access to storage panel."""
    return {
        "package_id": "storage_manager",
        "package_title": "Storage Manager",
        "package_name": "Storage Manager",
        "title": "Storage Manager",
        "name": "Storage Manager",
        "root_url": "/storage/dashboard",
        "resources": [
            {"url": "/static/tailwind.min.js", "type": "js"},
            {"url": "/static/htmx.min.js", "type": "js"},
            {"url": "/storage/dashboard", "type": "html"},
        ],
    }
