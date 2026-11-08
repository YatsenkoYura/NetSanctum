from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class MailFolder(Base):
    """A mail folder (inbox, sent, drafts, trash, spam, archive, custom)."""

    __tablename__ = "mail_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    is_system: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    messages = relationship("MailMessage", back_populates="folder", cascade="all, delete-orphan")


class MailMessage(Base):
    """A stored email message with threading support."""

    __tablename__ = "mail_messages"
    __table_args__ = (
        Index("ix_mail_messages_starred", "is_starred"),
        Index("ix_mail_messages_read", "is_read"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("mail_folders.id", ondelete="SET NULL"), nullable=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    message_id: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    subject: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)

    sender_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    sender_email: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    recipient_names: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    recipient_emails: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_starred: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    has_attachments: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )

    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    folder = relationship("MailFolder", back_populates="messages")


class MailContact(Base):
    """A contact from the address book or auto-collected from messages."""

    __tablename__ = "mail_contacts"
    __table_args__ = (Index("ix_mail_contacts_trusted", "is_trusted"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_trusted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class MailAccount(Base):
    """External mailbox connection settings for a user."""

    __tablename__ = "mail_accounts"
    __table_args__ = (Index("ix_mail_accounts_email", "email_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email_address: Mapped[str] = mapped_column(String(256), nullable=False)

    imap_host: Mapped[str] = mapped_column(String(256), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, nullable=False, default=993, server_default=text("993"))
    imap_username: Mapped[str] = mapped_column(String(256), nullable=False)
    imap_password: Mapped[str] = mapped_column(Text, nullable=False)
    imap_use_ssl: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )

    smtp_host: Mapped[str] = mapped_column(String(256), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, nullable=False, default=587, server_default=text("587"))
    smtp_username: Mapped[str] = mapped_column(String(256), nullable=False)
    smtp_password: Mapped[str] = mapped_column(Text, nullable=False)
    smtp_use_tls: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class MailAttachment(Base):
    """Stored attachment (inline image or file) for a mail message."""

    __tablename__ = "mail_attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("mail_messages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(256), nullable=False, server_default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    is_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    cid: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    message = relationship("MailMessage")
