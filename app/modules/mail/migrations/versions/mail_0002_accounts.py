"""Add mail accounts for external mailbox connections.

Revision ID: mail_0002
Revises: mail_0001
"""

import sqlalchemy as sa

from alembic import op

revision = "mail_0002"
down_revision = "mail_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("email_address", sa.String(length=256), nullable=False),
        sa.Column("imap_host", sa.String(length=256), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("imap_username", sa.String(length=256), nullable=False),
        sa.Column("imap_password", sa.Text(), nullable=False),
        sa.Column("imap_use_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("smtp_host", sa.String(length=256), nullable=False),
        sa.Column("smtp_port", sa.Integer(), nullable=False, server_default="587"),
        sa.Column("smtp_username", sa.String(length=256), nullable=False),
        sa.Column("smtp_password", sa.Text(), nullable=False),
        sa.Column("smtp_use_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mail_accounts_user_id", "mail_accounts", ["user_id"])
    op.create_index("ix_mail_accounts_email", "mail_accounts", ["email_address"])


def downgrade() -> None:
    op.drop_index("ix_mail_accounts_email", table_name="mail_accounts")
    op.drop_index("ix_mail_accounts_user_id", table_name="mail_accounts")
    op.drop_table("mail_accounts")
