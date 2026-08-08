import logging
from collections.abc import Sequence

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.storage import get_storage
from app.modules.video_archiver.models import (
    ArchivedVideo,
    VideoChannel,
    VideoPlaylist,
    video_playlist_association,
)
from app.modules.video_archiver.providers import PlatformRegistry

logger = logging.getLogger(__name__)

# Re-export PlatformRegistry as PlatformDetector for backward compatibility
PlatformDetector = PlatformRegistry


class VideoService:
    """Service layer for archived video operations."""

    @staticmethod
    async def list_videos(
        db: AsyncSession,
        search: str | None = None,
        channel_id: str | None = None,
        platform: str | None = None,
        status: str | None = None,
        is_deleted: bool | None = None,
        sort_by: str = "archived_at",
    ) -> Sequence[ArchivedVideo]:
        query = select(ArchivedVideo)
        if search:
            query = query.where(
                ArchivedVideo.title.ilike(f"%{search}%") | ArchivedVideo.channel_name.ilike(f"%{search}%")
            )
        if channel_id:
            query = query.where(
                (ArchivedVideo.channel_id == channel_id) | (ArchivedVideo.channel_name == channel_id)
            )
        if platform:
            query = query.where(ArchivedVideo.platform == platform)
        if status:
            query = query.where(ArchivedVideo.status == status)
        if is_deleted is not None:
            query = query.where(ArchivedVideo.is_deleted_on_youtube == is_deleted)

        if sort_by == "updated_at":
            query = query.order_by(
                ArchivedVideo.updated_at.desc().nullslast(), ArchivedVideo.archived_at.desc()
            )
        else:
            query = query.order_by(ArchivedVideo.archived_at.desc())
        res = await db.execute(query)
        return res.scalars().all()

    @staticmethod
    async def get_video(db: AsyncSession, video_id: str) -> ArchivedVideo:
        video = await db.get(ArchivedVideo, video_id)
        if not video:
            raise HTTPException(status_code=404, detail="Video not found")
        return video

    @staticmethod
    async def delete_video(db: AsyncSession, video_id: str) -> dict:
        video = await db.get(ArchivedVideo, video_id)
        if not video:
            raise HTTPException(status_code=404, detail="Video not found")

        storage = get_storage()
        if video.file_path:
            storage.delete_file(video.file_path)
        if video.thumbnail_path:
            storage.delete_file(video.thumbnail_path)
        if video.channel_avatar_url:
            storage.delete_file(video.channel_avatar_url)

        if video.subtitles and isinstance(video.subtitles, dict):
            for sub_path in video.subtitles.values():
                storage.delete_file(sub_path)

        await db.delete(video)
        await db.commit()
        return {"status": "success", "message": f"Deleted video {video_id}"}


class ChannelService:
    """Service layer for video channels."""

    @staticmethod
    async def list_channels(db: AsyncSession, platform: str | None = None) -> list[dict]:
        stmt = (
            select(
                VideoChannel.id,
                VideoChannel.name,
                VideoChannel.platform,
                VideoChannel.custom_url,
                VideoChannel.description,
                VideoChannel.avatar_path,
                func.count(ArchivedVideo.id).label("video_count"),
            )
            .outerjoin(ArchivedVideo, ArchivedVideo.channel_id == VideoChannel.id)
            .group_by(VideoChannel.id)
        )
        if platform:
            stmt = stmt.where(VideoChannel.platform == platform)

        stmt = stmt.order_by(VideoChannel.name)
        res = await db.execute(stmt)
        rows = res.all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "platform": r.platform,
                "custom_url": r.custom_url,
                "description": r.description,
                "avatar_path": r.avatar_path,
                "video_count": r.video_count,
            }
            for r in rows
        ]

    @staticmethod
    async def get_channel(db: AsyncSession, channel_id: str) -> VideoChannel:
        ch = await db.get(VideoChannel, channel_id)
        if not ch:
            raise HTTPException(status_code=404, detail="Channel not found")
        return ch


class PlaylistService:
    """Service layer for playlists."""

    @staticmethod
    async def list_playlists(db: AsyncSession) -> Sequence[VideoPlaylist]:
        stmt = select(VideoPlaylist).order_by(VideoPlaylist.created_at.desc())
        res = await db.execute(stmt)
        return res.scalars().all()

    @staticmethod
    async def get_playlist_videos(db: AsyncSession, playlist_id: int) -> Sequence[ArchivedVideo]:
        stmt = (
            select(ArchivedVideo)
            .join(video_playlist_association)
            .where(video_playlist_association.c.playlist_id == playlist_id)
            .order_by(ArchivedVideo.archived_at.desc())
        )
        res = await db.execute(stmt)
        return res.scalars().all()

    @staticmethod
    async def create_playlist(db: AsyncSession, name: str, description: str | None = None) -> VideoPlaylist:
        playlist = VideoPlaylist(name=name, description=description)
        db.add(playlist)
        await db.commit()
        await db.refresh(playlist)
        return playlist

    @staticmethod
    async def delete_playlist(db: AsyncSession, playlist_id: int) -> dict:
        playlist = await db.get(VideoPlaylist, playlist_id)
        if not playlist:
            raise HTTPException(status_code=404, detail="Playlist not found")
        await db.delete(playlist)
        await db.commit()
        return {"status": "success", "message": f"Deleted playlist {playlist_id}"}

    @staticmethod
    async def add_video(db: AsyncSession, playlist_id: int, video_id: str) -> dict:
        playlist = await db.get(VideoPlaylist, playlist_id)
        video = await db.get(ArchivedVideo, video_id)

        if not playlist or not video:
            raise HTTPException(status_code=404, detail="Playlist or video not found")

        stmt = select(video_playlist_association).where(
            (video_playlist_association.c.playlist_id == playlist_id)
            & (video_playlist_association.c.video_id == video_id)
        )
        res = await db.execute(stmt)
        if res.first():
            return {"status": "already_added"}

        stmt_ins = video_playlist_association.insert().values(playlist_id=playlist_id, video_id=video_id)
        await db.execute(stmt_ins)
        await db.commit()
        return {"status": "success"}

    @staticmethod
    async def remove_video(db: AsyncSession, playlist_id: int, video_id: str) -> dict:
        stmt = delete(video_playlist_association).where(
            (video_playlist_association.c.playlist_id == playlist_id)
            & (video_playlist_association.c.video_id == video_id)
        )
        await db.execute(stmt)
        await db.commit()
        return {"status": "success"}
