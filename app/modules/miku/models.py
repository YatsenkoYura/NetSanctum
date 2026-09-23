from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MikuTurnAudit(Base):
    """Metadata-only audit record; user text and integration data are never stored here."""

    __tablename__ = "miku_turn_audit"
    __table_args__ = (Index("ix_miku_turn_audit_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transport: Mapped[str] = mapped_column(String(16), nullable=False)
    command: Mapped[str] = mapped_column(String(16), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
