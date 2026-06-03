from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime

class DownloadRequest(BaseModel):
    url: str
    quality: str = "720"  # "720", "480", "1080"
    comments_enabled: bool = True
    comments_type: str = "top"  # "top" or "all"
    comments_limit: int = 20
    comments_replies: bool = True
    replies_limit: int = 5
    auto_update: bool = False
    cookies_text: Optional[str] = None

class PlaylistCreate(BaseModel):
    name: str
    description: Optional[str] = None

class CommentSchema(BaseModel):
    author: str
    text: str
    likes: int
    time: str
    replies: Optional[List["CommentSchema"]] = None

class VideoResponse(BaseModel):
    id: str
    title: str
    description: Optional[str]
    channel_name: str
    channel_id: str
    channel_avatar_url: Optional[str]
    duration: int
    resolution: str
    file_path: Optional[str]
    thumbnail_path: Optional[str]
    status: str
    comments: Optional[List[CommentSchema]]
    archived_at: datetime
    original_publish_date: Optional[datetime]
    auto_update: bool
    is_deleted_on_youtube: bool

    class Config:
        from_attributes = True

class PlaylistResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    videos: List[VideoResponse]

    class Config:
        from_attributes = True
