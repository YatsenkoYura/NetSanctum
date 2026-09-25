from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, FetchedValue, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SearchDocumentIndex(Base):
    __tablename__ = "search_documents"
    __table_args__ = (
        UniqueConstraint(
            "source_integration_id",
            "document_id",
            name="uq_search_documents_source_document",
        ),
        Index("ix_search_documents_module_entity", "source_module_id", "entity_type"),
        Index("ix_search_documents_normalized_title", "normalized_title"),
        Index("ix_search_documents_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_search_documents_trgm_title",
            "normalized_title",
            postgresql_using="gin",
            postgresql_ops={"normalized_title": "gin_trgm_ops"},
        ),
        Index(
            "ix_search_documents_trgm_keywords",
            "keywords_text",
            postgresql_using="gin",
            postgresql_ops={"keywords_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_module_id: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    source_integration_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    keywords_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalized_title: Mapped[str] = mapped_column(String(255), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    open_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    playable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    readable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    generation: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    search_vector: Mapped[Any | None] = mapped_column(
        TSVECTOR().with_variant(Text, "sqlite"),
        server_default=FetchedValue(),
        nullable=True,
    )


class SearchSyncState(Base):
    __tablename__ = "search_sync_state"

    source_integration_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_module_id: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    generation: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    document_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SearchRefreshOutbox(Base):
    __tablename__ = "search_refresh_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_module_id: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
