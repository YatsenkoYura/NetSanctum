import urllib.parse
from pathlib import PurePosixPath

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.responses import serve_media_stream, serve_storage_file_chunked
from app.core.templates import templates
from app.modules.video_archiver.models import ArchivedVideo, VideoChannel


class VideoShareProvider:
    selector_key = "video_ids"

    async def list_content(self, db: AsyncSession) -> list[dict]:
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

    async def validate_selection(
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

    async def render(self, request: Request, share, db: AsyncSession):
        videos = await self._selected_videos(db, share)
        return templates.TemplateResponse(
            request,
            "shared_video.html",
            {
                "share": share,
                "videos": videos,
                "lang": request.cookies.get("lang", "en"),
            },
        )

    async def serve_asset(
        self,
        request: Request,
        share,
        db: AsyncSession,
        resource_id: str,
        asset: str,
        argument: str | None = None,
    ):
        video = await self._get_allowed_video(db, share, resource_id)
        if asset == "stream":
            if not video.file_path:
                raise HTTPException(status_code=404, detail="Video file not found")
            response = serve_media_stream(request, video.file_path)
        elif asset == "download":
            if not share.allow_download or not video.file_path:
                raise HTTPException(status_code=404, detail="Shared content not found")
            response = serve_storage_file_chunked(video.file_path)
            suffix = PurePosixPath(video.file_path).suffix
            filename = urllib.parse.quote(f"{video.title}{suffix}")
            response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
        elif asset == "thumbnail":
            if not video.thumbnail_path:
                raise HTTPException(status_code=404, detail="Thumbnail not found")
            response = serve_storage_file_chunked(video.thumbnail_path)
        elif asset == "avatar":
            avatar_path = video.channel_avatar_url
            if not avatar_path and video.channel_id:
                channel = await db.get(VideoChannel, video.channel_id)
                avatar_path = channel.avatar_path if channel else None
            if not avatar_path:
                raise HTTPException(status_code=404, detail="Avatar not found")
            response = serve_storage_file_chunked(avatar_path)
        elif asset == "subtitle":
            if not argument or not isinstance(video.subtitles, dict) or argument not in video.subtitles:
                raise HTTPException(status_code=404, detail="Subtitle not found")
            response = serve_storage_file_chunked(video.subtitles[argument])
        else:
            raise HTTPException(status_code=404, detail="Shared content not found")

        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


PROVIDER = VideoShareProvider()
