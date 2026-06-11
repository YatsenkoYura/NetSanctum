from datetime import datetime

from pydantic import BaseModel


class DownloadRequest(BaseModel):
    url: str
    quality: str = "720"  # "720", "480", "1080"
    comments_enabled: bool = True
    comments_type: str = "top"  # "top" or "all"
    comments_limit: int = 20
    comments_replies: bool = True
    replies_limit: int = 5
    auto_update: bool = False
    cookies_text: str | None = None
    compress_video: bool = False
    download_subtitles: bool = False


class PlaylistCreate(BaseModel):
    name: str
    description: str | None = None


class CommentSchema(BaseModel):
    author: str
    text: str
    likes: int
    time: str
    replies: list["CommentSchema"] | None = None


class VideoResponse(BaseModel):
    id: str
    title: str
    description: str | None
    channel_name: str
    channel_id: str
    channel_avatar_url: str | None
    duration: int
    resolution: str
    file_path: str | None
    thumbnail_path: str | None
    status: str
    comments: list[CommentSchema] | None
    subtitles: dict | None = None
    archived_at: datetime
    original_publish_date: datetime | None
    auto_update: bool
    is_deleted_on_youtube: bool

    class Config:
        from_attributes = True


class PlaylistResponse(BaseModel):
    id: int
    name: str
    description: str | None
    created_at: datetime
    videos: list[VideoResponse]

    class Config:
        from_attributes = True
