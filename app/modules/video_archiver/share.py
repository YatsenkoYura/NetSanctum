import urllib.parse
from pathlib import PurePosixPath

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.module_types import ShareAsset, ShareRoute
from app.core.responses import serve_media_stream, serve_storage_file_chunked
from app.modules.video_archiver.models import (
    ArchivedVideo,
    VideoChannel,
    VideoPlaylist,
    video_playlist_association,
)


class VideoShareProvider:
    async def catalog(self, db: AsyncSession) -> list[dict]:
        result = await db.execute(select(ArchivedVideo).order_by(ArchivedVideo.archived_at.desc()))
        return [
            {
                "id": video.id,
                "title": video.title,
                "channel_name": video.channel_name,
                "platform": video.platform,
                "duration": video.duration,
                "status": video.status,
                "has_file": bool(video.file_path),
            }
            for video in result.scalars().all()
        ]

    async def selection(
        self,
        db: AsyncSession,
        selection_mode: str,
        selector: dict,
    ) -> dict:
        if selection_mode == "all":
            return {}

        video_ids = selector.get("video_ids")
        if not isinstance(video_ids, list) or not video_ids:
            raise HTTPException(status_code=422, detail="Select at least one video")
        if len(video_ids) > 500:
            raise HTTPException(status_code=422, detail="A share may contain at most 500 videos")

        normalized_ids = []
        for video_id in video_ids:
            if not isinstance(video_id, str) or not video_id or len(video_id) > 255:
                raise HTTPException(status_code=422, detail="Invalid video ID")
            if video_id not in normalized_ids:
                normalized_ids.append(video_id)

        result = await db.execute(select(ArchivedVideo.id).where(ArchivedVideo.id.in_(normalized_ids)))
        found_ids = set(result.scalars().all())
        if missing := set(normalized_ids) - found_ids:
            raise HTTPException(status_code=422, detail=f"Videos not found: {', '.join(sorted(missing))}")
        return {"video_ids": normalized_ids}

    @staticmethod
    def _is_allowed(share, video_id: str) -> bool:
        return share.selection_mode == "all" or video_id in share.selector.get("video_ids", [])

    async def _get_allowed_video(self, db: AsyncSession, share, video_id: str) -> ArchivedVideo:
        if not self._is_allowed(share, video_id):
            raise HTTPException(status_code=404, detail="Shared content not found")
        video = await db.get(ArchivedVideo, video_id)
        if not video:
            raise HTTPException(status_code=404, detail="Shared content not found")
        return video

    async def _selected_videos(self, db: AsyncSession, share) -> list[ArchivedVideo]:
        if share.selection_mode == "all":
            result = await db.execute(select(ArchivedVideo).order_by(ArchivedVideo.archived_at.desc()))
            return list(result.scalars().all())

        video_ids = share.selector.get("video_ids", [])
        result = await db.execute(select(ArchivedVideo).where(ArchivedVideo.id.in_(video_ids)))
        videos = {video.id: video for video in result.scalars().all()}
        return [videos[video_id] for video_id in video_ids if video_id in videos]

    @staticmethod
    def _serialize_video(video: ArchivedVideo) -> dict:
        return {
            "id": video.id,
            "title": video.title,
            "description": video.description,
            "platform": video.platform,
            "channel_id": video.channel_id,
            "channel_name": video.channel_name,
            "channel_avatar_url": bool(video.channel_avatar_url or video.channel_id),
            "duration": video.duration,
            "resolution": video.resolution,
            "file_path": bool(video.file_path),
            "thumbnail_path": bool(video.thumbnail_path),
            "status": video.status,
            "comments": video.comments or [],
            "subtitles": dict.fromkeys(video.subtitles or {}, True),
            "like_count": video.like_count,
            "view_count": video.view_count,
            "tags": video.tags or [],
            "archived_at": video.archived_at,
            "updated_at": video.updated_at,
            "original_publish_date": video.original_publish_date,
            "auto_update": False,
            "is_deleted_on_youtube": video.is_deleted_on_youtube,
        }

    async def _shared_playlists(self, db: AsyncSession, share) -> list[dict]:
        videos = await self._selected_videos(db, share)
        allowed_ids = {video.id for video in videos}
        if not allowed_ids:
            return []
        result = await db.execute(
            select(VideoPlaylist, video_playlist_association.c.video_id)
            .join(
                video_playlist_association,
                VideoPlaylist.id == video_playlist_association.c.playlist_id,
            )
            .where(video_playlist_association.c.video_id.in_(allowed_ids))
            .order_by(VideoPlaylist.created_at.desc())
        )
        playlists: dict[int, dict] = {}
        for playlist, video_id in result.all():
            item = playlists.setdefault(
                playlist.id,
                {
                    "id": playlist.id,
                    "name": playlist.name,
                    "description": playlist.description,
                    "created_at": playlist.created_at,
                    "video_ids": [],
                },
            )
            item["video_ids"].append(video_id)
        return list(playlists.values())

    async def entities(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name == "videos":
            videos = await self._selected_videos(db, share)
            search = (request.query_params.get("search") or "").lower()
            channel_id = request.query_params.get("channel_id")
            status = request.query_params.get("status")
            deleted = request.query_params.get("is_deleted")
            if search:
                videos = [
                    video
                    for video in videos
                    if search in video.title.lower()
                    or search in (video.description or "").lower()
                    or search in video.channel_name.lower()
                ]
            if channel_id:
                videos = [
                    video
                    for video in videos
                    if video.channel_id == channel_id or video.channel_name == channel_id
                ]
            if status:
                videos = [video for video in videos if video.status == status]
            if deleted in {"true", "false"}:
                expected = deleted == "true"
                videos = [video for video in videos if video.is_deleted_on_youtube is expected]
            if request.query_params.get("sort_by") == "updated_at":
                videos.sort(key=lambda video: video.updated_at or video.archived_at, reverse=True)
            return [self._serialize_video(video) for video in videos]
        if route.name == "video_detail":
            video = await self._get_allowed_video(db, share, str(params["video_id"]))
            return self._serialize_video(video)
        raise HTTPException(status_code=404, detail="Shared entity route not found")

    async def relations(
        self,
        request: Request,
        share,
        db: AsyncSession,
        route: ShareRoute,
        params: dict,
    ):
        if route.name == "playlists":
            return await self._shared_playlists(db, share)
        if route.name == "tasks_active":
            return []
        if route.name == "sync_dates":
            return {"never": 0, "dates": [], "platforms": []}
        if route.name == "cookies":
            return {
                "platform": params["platform"],
                "cookies_text": "",
                "has_cookies": False,
                "auth_active": False,
            }
        if route.name == "youtube_oauth_status":
            return {"status": "not_found"}
        if route.name == "playlist_detail":
            playlist_id = int(params["playlist_id"])
            playlists = await self._shared_playlists(db, share)
            playlist = next((item for item in playlists if item["id"] == playlist_id), None)
            if not playlist:
                raise HTTPException(status_code=404, detail="Shared playlist not found")
            videos = await self._selected_videos(db, share)
            video_map = {video.id: video for video in videos}
            return {
                **playlist,
                "videos": [
                    self._serialize_video(video_map[video_id])
                    for video_id in playlist["video_ids"]
                    if video_id in video_map
                ],
            }
        raise HTTPException(status_code=404, detail="Shared relation route not found")

    async def asset(
        self,
        request: Request,
        share,
        db: AsyncSession,
        asset: ShareAsset,
        params: dict,
    ):
        if asset.name == "channel_avatar":
            channel_id = str(params["channel_id"])
            videos = await self._selected_videos(db, share)
            video = next((item for item in videos if item.channel_id == channel_id), None)
            if not video:
                raise HTTPException(status_code=404, detail="Shared avatar not found")
            asset_name = "video_avatar"
        else:
            video = await self._get_allowed_video(db, share, str(params["video_id"]))
            asset_name = asset.name

        if asset_name == "video_stream":
            if not video.file_path:
                raise HTTPException(status_code=404, detail="Video file not found")
            response = serve_media_stream(request, video.file_path)
        elif asset_name == "video_download":
            if not share.allow_download or not video.file_path:
                raise HTTPException(status_code=404, detail="Shared content not found")
            response = serve_storage_file_chunked(video.file_path)
            suffix = PurePosixPath(video.file_path).suffix
            filename = urllib.parse.quote(f"{video.title}{suffix}")
            response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
        elif asset_name == "video_thumbnail":
            if not video.thumbnail_path:
                raise HTTPException(status_code=404, detail="Thumbnail not found")
            response = serve_storage_file_chunked(video.thumbnail_path)
        elif asset_name == "video_avatar":
            avatar_path = video.channel_avatar_url
            if not avatar_path and video.channel_id:
                channel = await db.get(VideoChannel, video.channel_id)
                avatar_path = channel.avatar_path if channel else None
            if not avatar_path:
                raise HTTPException(status_code=404, detail="Avatar not found")
            response = serve_storage_file_chunked(avatar_path)
        elif asset_name == "video_subtitle":
            language = str(params["language"])
            if not isinstance(video.subtitles, dict) or language not in video.subtitles:
                raise HTTPException(status_code=404, detail="Subtitle not found")
            response = serve_storage_file_chunked(video.subtitles[language])
        else:
            raise HTTPException(status_code=404, detail="Shared content not found")

        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


PROVIDER = VideoShareProvider()
