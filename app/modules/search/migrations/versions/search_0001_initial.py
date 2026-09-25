"""Search index baseline.

Revision ID: search_0001
Revises:
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op

revision = "search_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    if is_postgres:
        op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm;"))

    op.create_table(
        "search_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_module_id", sa.String(length=63), nullable=False),
        sa.Column("source_integration_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=255), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("subtitle", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("keywords_text", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.String(length=255), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("open_path", sa.String(length=1000), nullable=True),
        sa.Column("playable", sa.Boolean(), nullable=False),
        sa.Column("readable", sa.Boolean(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.String(length=36), nullable=False),
        *(
            [
                sa.Column(
                    "search_vector",
                    TSVECTOR(),
                    sa.Computed(
                        """(
                            setweight(to_tsvector('russian', coalesce(title, '')), 'A') ||
                            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
                            setweight(to_tsvector('russian', coalesce(keywords_text, '')), 'B') ||
                            setweight(to_tsvector('english', coalesce(keywords_text, '')), 'B') ||
                            setweight(to_tsvector('russian', coalesce(subtitle, '')), 'C') ||
                            setweight(to_tsvector('english', coalesce(subtitle, '')), 'C') ||
                            setweight(to_tsvector('russian', coalesce(body, '')), 'D') ||
                            setweight(to_tsvector('english', coalesce(body, '')), 'D')
                        )""",
                        persisted=True,
                    ),
                    nullable=True,
                )
            ]
            if is_postgres
            else [sa.Column("search_vector", sa.Text(), nullable=True)]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_integration_id",
            "document_id",
            name="uq_search_documents_source_document",
        ),
    )
    op.create_index("ix_search_documents_entity_type", "search_documents", ["entity_type"])
    op.create_index("ix_search_documents_generation", "search_documents", ["generation"])
    op.create_index(
        "ix_search_documents_module_entity", "search_documents", ["source_module_id", "entity_type"]
    )
    op.create_index("ix_search_documents_normalized_title", "search_documents", ["normalized_title"])
    op.create_index(
        "ix_search_documents_source_integration_id", "search_documents", ["source_integration_id"]
    )
    op.create_index("ix_search_documents_source_module_id", "search_documents", ["source_module_id"])
    if is_postgres:
        op.create_index(
            "ix_search_documents_search_vector",
            "search_documents",
            ["search_vector"],
            postgresql_using="gin",
        )
        op.create_index(
            "ix_search_documents_trgm_title",
            "search_documents",
            ["normalized_title"],
            postgresql_using="gin",
            postgresql_ops={"normalized_title": "gin_trgm_ops"},
        )
        op.create_index(
            "ix_search_documents_trgm_keywords",
            "search_documents",
            ["keywords_text"],
            postgresql_using="gin",
            postgresql_ops={"keywords_text": "gin_trgm_ops"},
        )
    op.create_table(
        "search_sync_state",
        sa.Column("source_integration_id", sa.String(length=128), nullable=False),
        sa.Column("source_module_id", sa.String(length=63), nullable=False),
        sa.Column("generation", sa.String(length=36), nullable=True),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("source_integration_id"),
    )
    op.create_index("ix_search_sync_state_source_module_id", "search_sync_state", ["source_module_id"])
    op.create_table(
        "search_refresh_outbox",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_module_id", sa.String(length=63), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_search_refresh_outbox_source_module_id",
        "search_refresh_outbox",
        ["source_module_id"],
    )


def downgrade() -> None:
    op.drop_table("search_refresh_outbox")
    op.drop_table("search_sync_state")
    op.drop_table("search_documents")
