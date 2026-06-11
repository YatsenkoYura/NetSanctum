"""
RanobeLib module database models.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RanobeNovel(Base):
    """Represents a downloaded light novel."""

    __tablename__ = "ranobe_novels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    rus_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    eng_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_path: Mapped[str | None] = mapped_column(String(510), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(510), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    chapters: Mapped[list["RanobeChapter"]] = relationship(
        "RanobeChapter",
        back_populates="novel",
        cascade="all, delete-orphan",
        order_by="RanobeChapter.volume_int, RanobeChapter.number_float",
    )

    def __repr__(self) -> str:
        return f"<RanobeNovel id={self.id} title={self.title!r} slug={self.slug!r}>"


class RanobeChapter(Base):
    """Represents a single chapter of a light novel."""

    __tablename__ = "ranobe_chapters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    novel_id: Mapped[int] = mapped_column(ForeignKey("ranobe_novels.id", ondelete="CASCADE"), nullable=False)
    volume: Mapped[str] = mapped_column(String(50), nullable=False)
    number: Mapped[str] = mapped_column(String(50), nullable=False)

    # helper fields for correct ordering
    volume_int: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    number_float: Mapped[float] = mapped_column(nullable=False, default=0.0)

    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_html: Mapped[str | None] = mapped_column(Text, nullable=True)

    novel: Mapped["RanobeNovel"] = relationship("RanobeNovel", back_populates="chapters")

    def __repr__(self) -> str:
        return f"<RanobeChapter id={self.id} novel_id={self.novel_id} vol={self.volume} num={self.number}>"
