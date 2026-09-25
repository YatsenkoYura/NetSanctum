from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Postgres stores cascades as JSONB; other backends fall back to plain JSON.
JSON_VARIANT = JSONB().with_variant(JSON(), "sqlite")


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


class MikuProfileMemory(Base):
    """Durable user-specific facts and preferences explicitly remembered by MIKU."""

    __tablename__ = "miku_profile_memory"
    __table_args__ = (
        Index("ix_miku_profile_memory_user_key", "user_id", "memory_key"),
        Index("ix_miku_profile_memory_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    memory_key: Mapped[str] = mapped_column(String(64), nullable=False)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="explicit")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MikuEpisodeMemory(Base):
    """Compact long-lived event summaries that help MIKU recall prior interactions."""

    __tablename__ = "miku_episode_memory"
    __table_args__ = (
        Index("ix_miku_episode_memory_user_created", "user_id", "created_at"),
        Index("ix_miku_episode_memory_user_occurred", "user_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(160), nullable=True)
    tags_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_module_id: Mapped[str | None] = mapped_column(String(63), nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="derived")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class MikuCascadeLog(Base):
    """One executed cascade: what was asked, what ran, and how to undo it.

    The steps are stored as JSONB so a finished run can be replayed as few-shot
    material for the planner, and so a mutating step can be undone later.
    """

    __tablename__ = "miku_cascade_log"
    __table_args__ = (Index("ix_miku_cascade_log_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    steps_json: Mapped[dict] = mapped_column(JSON_VARIANT, nullable=False, default=dict)
    skeleton_json: Mapped[list] = mapped_column(JSON_VARIANT, nullable=False, default=list)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    undo_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class MikuTask(Base):
    """An unfinished goal the agent can come back to on its own."""

    __tablename__ = "miku_task"
    __table_args__ = (Index("ix_miku_task_state_due", "state", "due_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    cursor_json: Mapped[dict | None] = mapped_column(JSON_VARIANT, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
