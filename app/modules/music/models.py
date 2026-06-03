"""
Music module database models.
"""

from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PlaylistSong(Base):
    """Association table for Many-to-Many relationship with ordering."""
    __tablename__ = "playlist_songs"
    
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"), primary_key=True)
    song_id: Mapped[int] = mapped_column(ForeignKey("songs.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    playlist: Mapped["Playlist"] = relationship(back_populates="playlist_songs")
    song: Mapped["Song"] = relationship(back_populates="playlist_songs")


class Playlist(Base):
    """Playlist containing multiple songs."""

    __tablename__ = "playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    playlist_songs: Mapped[list["PlaylistSong"]] = relationship(
        "PlaylistSong", back_populates="playlist", cascade="all, delete-orphan", order_by="PlaylistSong.position"
    )

    def __repr__(self) -> str:
        return f"<Playlist id={self.id} name={self.name!r}>"


class Song(Base):
    """Individual downloaded song with AI-analyzed metadata."""

    __tablename__ = "songs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_artist: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cover_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    audio_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    youtube_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    playlist_songs: Mapped[list["PlaylistSong"]] = relationship(
        "PlaylistSong", back_populates="song", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Song id={self.id} title={self.title!r}>"
