import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Table
from sqlalchemy.orm import relationship

from app.core.database import Base

# Many-to-Many association for custom playlists
video_playlist_association = Table(
    "video_playlist_association",
    Base.metadata,
    Column("video_id", String, ForeignKey("archived_videos.id", ondelete="CASCADE"), primary_key=True),
    Column("playlist_id", Integer, ForeignKey("video_playlists.id", ondelete="CASCADE"), primary_key=True),
)


class ArchivedVideo(Base):
    __tablename__ = "archived_videos"

    id = Column(String, primary_key=True)  # YouTube Video ID (e.g., dQw4w9WgXcQ)
    title = Column(String, nullable=False)
    description = Column(String, nullable=True)
    channel_name = Column(String, nullable=False)
    channel_id = Column(String, nullable=False)
    channel_avatar_url = Column(String, nullable=True)  # Path to local/remote avatar

    duration = Column(Integer, nullable=False)  # In seconds
    resolution = Column(String, nullable=False)  # E.g., "720p", "480p"
    file_path = Column(String, nullable=True)  # Local path inside storage/video_archiver/videos/
    thumbnail_path = Column(String, nullable=True)  # Local path to cached thumbnail image

    status = Column(String, default="pending")  # pending, downloading, completed, failed
    comments = Column(JSON, nullable=True)  # JSON list of dicts: [{author, text, likes, time}]
    subtitles = Column(JSON, nullable=True)  # JSON mapping of lang to file path: {"ru": "...", "en": "..."}

    like_count = Column(Integer, nullable=True)  # Total likes on YouTube
    view_count = Column(Integer, nullable=True)  # Total views on YouTube
    tags = Column(JSON, nullable=True)  # List of tags (strings)

    archived_at = Column(DateTime, default=datetime.datetime.utcnow)
    original_publish_date = Column(DateTime, nullable=True)

    auto_update = Column(Boolean, default=False)
    is_deleted_on_youtube = Column(Boolean, default=False)  # Flag if video got deleted/privated on YT

    playlists = relationship("VideoPlaylist", secondary=video_playlist_association, back_populates="videos")


class VideoPlaylist(Base):
    __tablename__ = "video_playlists"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    videos = relationship("ArchivedVideo", secondary=video_playlist_association, back_populates="playlists")
