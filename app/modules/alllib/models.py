"""
Database models for Lib network modules (RanobeLib, MangaLib, HentaiLib, etc.).
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.core.database import Base


class LibMedia(Base):
    """Represents a downloaded media item (novel, manga, anime)."""

    __tablename__ = "lib_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    media_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="novel"
    )  # "novel", "manga", "anime"

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    rus_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    eng_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_path: Mapped[str | None] = mapped_column(String(510), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(510), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    chapters: Mapped[list["LibChapter"]] = relationship(
        "LibChapter",
        back_populates="media",
        cascade="all, delete-orphan",
        order_by="LibChapter.volume_int, LibChapter.number_float",
    )

    def __repr__(self) -> str:
        return f"<LibMedia id={self.id} title={self.title!r} slug={self.slug!r} type={self.media_type}>"


class LibChapter(Base):
    """Represents a single chapter of media."""

    __tablename__ = "lib_chapters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_id: Mapped[int] = mapped_column(ForeignKey("lib_media.id", ondelete="CASCADE"), nullable=False)
    novel_id = synonym("media_id")

    volume: Mapped[str] = mapped_column(String(50), nullable=False)
    number: Mapped[str] = mapped_column(String(50), nullable=False)

    volume_int: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    number_float: Mapped[float] = mapped_column(nullable=False, default=0.0)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Novel-specific content (HTML text)
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Manga-specific content (JSON list of file paths)
    pages_list: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    # Anime-specific content (path to local video file)
    video_path: Mapped[str | None] = mapped_column(String(510), nullable=True)

    media: Mapped["LibMedia"] = relationship("LibMedia", back_populates="chapters")
    novel = synonym("media")

    def __repr__(self) -> str:
        return f"<LibChapter id={self.id} media_id={self.media_id} vol={self.volume} num={self.number}>"
