"""Search vector and trigram indexes.

These indexes may already exist in an adopted database, where the baseline was created
straight from the model metadata, so every statement is written to be re-runnable.

Revision ID: search_0002
Revises: search_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "search_0002"
down_revision = "search_0001"
branch_labels = None
depends_on = None

GIN_INDEXES = (
    ("ix_search_documents_search_vector", "search_vector", None),
    ("ix_search_documents_trgm_title", "normalized_title", "gin_trgm_ops"),
    ("ix_search_documents_trgm_keywords", "keywords_text", "gin_trgm_ops"),
)


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    for name, column, opclass in GIN_INDEXES:
        if is_postgres:
            using = f"USING gin ({column} {opclass})" if opclass else f"USING gin ({column})"
            op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {name} ON search_documents {using};"))
        else:
            op.create_index(name, "search_documents", [column])


def downgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    for name, _column, _opclass in reversed(GIN_INDEXES):
        if is_postgres:
            op.execute(sa.text(f"DROP INDEX IF EXISTS {name};"))
        else:
            op.drop_index(name, table_name="search_documents")
