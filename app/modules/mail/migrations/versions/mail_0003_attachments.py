"""Add mail attachments table for inline images and file attachments.

Revision ID: mail_0003
Revises: mail_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "mail_0003"
down_revision = "mail_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_attachments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=256), nullable=False, server_default="application/octet-stream"),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_inline", sa.Boolean(), nullable=False, server_default="f"),
        sa.Column("cid", sa.String(length=256), nullable=True),
        sa.Column("data", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["mail_messages.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_mail_attachments_user_id", "mail_attachments", ["user_id"])
    op.create_index("ix_mail_attachments_message_id", "mail_attachments", ["message_id"])
    op.create_index("ix_mail_attachments_cid", "mail_attachments", ["cid"])


def downgrade() -> None:
    op.drop_index("ix_mail_attachments_cid", table_name="mail_attachments")
    op.drop_index("ix_mail_attachments_message_id", table_name="mail_attachments")
    op.drop_index("ix_mail_attachments_user_id", table_name="mail_attachments")
    op.drop_table("mail_attachments")
