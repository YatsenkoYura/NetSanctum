from datetime import datetime

from pydantic import BaseModel, Field

# --- Folder schemas ---


class FolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class FolderResponse(BaseModel):
    id: int
    user_id: int
    name: str
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- Attachment schemas ---


class AttachmentResponse(BaseModel):
    id: int
    message_id: int
    filename: str
    content_type: str
    size: int
    is_inline: bool
    cid: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Message schemas ---


class MessageCreate(BaseModel):
    """Create/send a new email message."""

    to: list[str] = Field(..., min_length=1)
    subject: str = Field(..., max_length=512)
    body_html: str | None = None
    body_text: str | None = None
    in_reply_to: str | None = None  # Message-ID for threading
    folder_id: int | None = None


class MessageUpdate(BaseModel):
    subject: str | None = None
    body_html: str | None = None
    body_text: str | None = None
    is_starred: bool | None = None
    is_read: bool | None = None
    folder_id: int | None = None


class MessageResponse(BaseModel):
    id: int
    user_id: int
    folder_id: int | None
    thread_id: str | None
    message_id: str | None
    in_reply_to: str | None
    subject: str | None
    sender_name: str | None
    sender_email: str | None
    recipient_names: str | None
    recipient_emails: str | None
    body_html: str | None
    body_text: str | None
    is_starred: bool
    is_read: bool
    has_attachments: bool
    received_at: datetime | None
    sent_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ThreadResponse(BaseModel):
    """A thread of messages."""

    thread_id: str
    subject: str
    messages: list[MessageResponse]
    last_at: datetime
    unread_count: int


# --- Contact schemas ---


class ContactCreate(BaseModel):
    email: str = Field(..., max_length=256)
    name: str | None = Field(None, max_length=256)


class ContactResponse(BaseModel):
    id: int
    user_id: int
    email: str
    name: str | None
    is_trusted: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# --- Account schemas ---


class MailAccountBase(BaseModel):
    name: str | None = Field(None, max_length=128)
    email_address: str = Field(..., max_length=256)
    imap_host: str = Field(..., max_length=256)
    imap_port: int = Field(993, ge=1, le=65535)
    imap_username: str = Field(..., max_length=256)
    imap_use_ssl: bool = True
    smtp_host: str = Field(..., max_length=256)
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_username: str = Field(..., max_length=256)
    smtp_use_tls: bool = True
    is_active: bool = True

    model_config = {"extra": "forbid"}


class MailAccountCreate(MailAccountBase):
    imap_password: str = Field(..., min_length=1, max_length=4096)
    smtp_password: str = Field(..., min_length=1, max_length=4096)


class MailAccountUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    email_address: str | None = Field(None, max_length=256)
    imap_host: str | None = Field(None, max_length=256)
    imap_port: int | None = Field(None, ge=1, le=65535)
    imap_username: str | None = Field(None, max_length=256)
    imap_password: str | None = Field(None, min_length=1, max_length=4096)
    imap_use_ssl: bool | None = None
    smtp_host: str | None = Field(None, max_length=256)
    smtp_port: int | None = Field(None, ge=1, le=65535)
    smtp_username: str | None = Field(None, max_length=256)
    smtp_password: str | None = Field(None, min_length=1, max_length=4096)
    smtp_use_tls: bool | None = None
    is_active: bool | None = None


class MailAccountResponse(MailAccountBase):
    id: int
    user_id: int
    last_synced_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# --- SMTP config (for sending) ---


class SmtpConfig(BaseModel):
    host: str = Field(..., max_length=256)
    port: int = Field(..., ge=1, le=65535)
    username: str = Field(..., max_length=256)
    password: str = Field(..., max_length=512)
    use_tls: bool = Field(default=True)


class SmtpConfigResponse(BaseModel):
    host: str
    port: int
    username: str
    use_tls: bool

    model_config = {"from_attributes": True}
