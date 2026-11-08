"""Create mail tables: folders, messages, contacts.

Revision ID: mail_0001
Revises: None
"""

import sqlalchemy as sa

from alembic import op

revision = "mail_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mail_folders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="f"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mail_folders_user_id", "mail_folders", ["user_id"])

    op.create_table(
        "mail_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("folder_id", sa.Integer(), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("message_id", sa.String(length=256), nullable=True),
        sa.Column("in_reply_to", sa.String(length=256), nullable=True),
        sa.Column("subject", sa.String(length=512), nullable=True),
        sa.Column("sender_name", sa.String(length=256), nullable=True),
        sa.Column("sender_email", sa.String(length=256), nullable=True),
        sa.Column("recipient_names", sa.String(length=1024), nullable=True),
        sa.Column("recipient_emails", sa.String(length=2048), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("raw_source", sa.Text(), nullable=True),
        sa.Column("is_starred", sa.Boolean(), nullable=False, server_default="f"),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default="f"),
        sa.Column("has_attachments", sa.Boolean(), nullable=False, server_default="f"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["folder_id"],
            ["mail_folders.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mail_messages_user_id", "mail_messages", ["user_id"])
    op.create_index("ix_mail_messages_thread_id", "mail_messages", ["thread_id"])
    op.create_index("ix_mail_messages_message_id", "mail_messages", ["message_id"])
    op.create_index("ix_mail_messages_in_reply_to", "mail_messages", ["in_reply_to"])
    op.create_index("ix_mail_messages_subject", "mail_messages", ["subject"])
    op.create_index("ix_mail_messages_sender_email", "mail_messages", ["sender_email"])
    op.create_index("ix_mail_messages_starred", "mail_messages", ["is_starred"])
    op.create_index("ix_mail_messages_read", "mail_messages", ["is_read"])

    op.create_table(
        "mail_contacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=True),
        sa.Column("is_trusted", sa.Boolean(), nullable=False, server_default="f"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mail_contacts_user_id", "mail_contacts", ["user_id"])
    op.create_index("ix_mail_contacts_email", "mail_contacts", ["email"])
    op.create_index("ix_mail_contacts_trusted", "mail_contacts", ["is_trusted"])


def downgrade() -> None:
    op.drop_table("mail_contacts")
    op.drop_table("mail_messages")
    op.drop_table("mail_folders")
