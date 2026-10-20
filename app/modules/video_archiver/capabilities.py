import re

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.video_archiver.models import ArchivedVideo
from app.modules.video_archiver.providers import PlatformRegistry

# Playlist ids are numeric and start at 1, mirroring the music module's
# `song_`/`playlist_` pattern: `video_playlist_007` and `video_playlist_0` are
# not spellings of playlist 7.
VIDEO_PLAYLIST_PACKAGE_PATTERN = re.compile(r"video_playlist_([1-9][0-9]*)")
VIDEO_PACKAGE_PATTERN = re.compile(r"video_(.+)")


async def resolve_package_resources(package_id: str, db: AsyncSession) -> list:
    """Return the offline resources of one video package.

    The id is the caller's input and is validated before anything is queried:
    a package id this module does not own, or one that does not resolve to the
    object it names, is a 400 — never a lookup under the wrong identity.
    """
    if package_id.startswith("video_playlist_"):
        match = VIDEO_PLAYLIST_PACKAGE_PATTERN.fullmatch(package_id)
        if not match:
            raise HTTPException(status_code=400, detail="Invalid video_archiver package ID")
        from app.modules.video_archiver.router import get_playlist_sync_manifest

        manifest = await get_playlist_sync_manifest(int(match.group(1)), db=db, hybrid=False)
    else:
        match = VIDEO_PACKAGE_PATTERN.fullmatch(package_id)
        if not match:
            raise HTTPException(status_code=400, detail="Invalid video_archiver package ID")
        from app.modules.video_archiver.router import get_video_sync_manifest

        manifest = await get_video_sync_manifest(match.group(1), db=db, hybrid=False)
    if manifest.get("package_id") != package_id:
        # The manifest names the package it built itself. A mismatch means the
        # id resolved to a different object, and serving under the wrong
        # identity would hand the client resources it never asked for.
        raise HTTPException(status_code=400, detail="Video package identity mismatch")
    return manifest.get("resources", [])


async def resolve_entity(db: AsyncSession, entity_type: str, entity_id: str) -> dict | None:
    video = await db.get(ArchivedVideo, entity_id)
    if not video:
        return None
    source_url = PlatformRegistry.get_provider_by_id(video.platform).build_video_url(video.id)
    return {
        "type": entity_type,
        "id": video.id,
        "title": video.title,
        "description": video.description,
        "author": video.channel_name,
        "duration": video.duration,
        "url": f"/video-archiver/dashboard?video_id={video.id}",
        "thumbnail": f"/api/video-archiver/videos/{video.id}/thumbnail" if video.thumbnail_path else None,
        "thumbnail_storage_path": video.thumbnail_path,
        "storage_path": video.file_path if video.status == "completed" else None,
        "source_url": source_url,
    }
