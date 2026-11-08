"""Business logic for the Mail module."""

import base64
import email
import imaplib
import re
import smtplib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses, parsedate_to_datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.mail_encryption import decrypt_or_raise, encrypt_secret
from app.modules.mail.models import MailAccount, MailAttachment, MailContact, MailFolder, MailMessage
from app.modules.mail.schemas import (
    FolderCreate,
    MailAccountCreate,
    MailAccountUpdate,
    MessageCreate,
    MessageUpdate,
)

SYSTEM_FOLDERS = ("inbox", "sent", "drafts", "trash", "spam", "archive")

MAX_INLINE_IMAGE_SIZE = 2 * 1024 * 1024  # 2 MB per inline image
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB per attachment

# Allowed HTML tags for email body sanitization (XSS prevention)
_ALLOWED_HTML_TAGS = frozenset(
    {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "i",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)

# Attributes allowed on <a> tag
_ALLOWED_A_ATTRS = frozenset({"href", "rel", "target", "title"})

# Attributes allowed on <img> tag
_ALLOWED_IMG_ATTRS = frozenset({"alt", "src", "width", "height"})


def _sanitize_html(html: str | None) -> str | None:
    """Strip dangerous HTML (script, event handlers, javascript: URIs) from email bodies."""
    if not html:
        return html

    # Remove <script>, <style>, <iframe>, <object>, <embed>, <form>, <link>, <meta>
    html = re.sub(
        r"<(script|style|iframe|object|embed|form|link|meta)[\s>].*?</\1>",
        "",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    html = re.sub(r"<(script|style|iframe|object|embed|form|link|meta)[\s>].*", "", html, flags=re.IGNORECASE)

    # Remove inline event handlers (on*=...)
    html = re.sub(r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|S*[^\s>]+)', "", html, flags=re.IGNORECASE)

    # Sanitize href/src: replace javascript: and data: with safe fallback
    html = re.sub(r'(href|src)\s*=\s*"javascript:"', r'\1="#"', html, flags=re.IGNORECASE)
    html = re.sub(r"(href|src)\s*=\s*'javascript:'", r"\1='#'", html, flags=re.IGNORECASE)
    html = re.sub(r"(href|src)\s*=\s*javascript:", r'\1="#"', html, flags=re.IGNORECASE)

    # Strip all attributes from disallowed tags, keep only allowed ones
    # Process <tag ...> -> <tag> if tag not allowed
    def _strip_disallowed_tags(match):
        full = match.group(0)
        tag_match = re.match(r"<(\w+)(\s[^>]*)?\s*/?>", full, re.IGNORECASE)
        if not tag_match:
            return full
        tag_name = tag_match.group(1).lower()
        if tag_name not in _ALLOWED_HTML_TAGS:
            return f"<{tag_name}>"
        # Tag is allowed — keep it with only whitelisted attributes
        attr_str = tag_match.group(2) or ""
        allowed_attrs = (
            _ALLOWED_A_ATTRS
            if tag_name == "a"
            else (_ALLOWED_IMG_ATTRS if tag_name == "img" else frozenset())
        )
        if not allowed_attrs:
            return f"<{tag_name}>"
        # Extract only allowed attributes
        attr_pattern = r'(\w[\w-]*)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))'
        kept = []
        for am in re.finditer(attr_pattern, attr_str, re.IGNORECASE):
            attr_name = am.group(1).lower()
            if attr_name in allowed_attrs:
                value = am.group(2) or am.group(3) or am.group(4)
                kept.append(f'{attr_name}="{value}"')
        attrs = " " + " ".join(kept) if kept else ""
        return f"<{tag_name}{attrs}>"

    html = re.sub(r"<\w+[\s>][^<]*>|<\w+[\s/]*>", _strip_disallowed_tags, html)

    return html


@dataclass
class _ExtractedBody:
    body_html: str | None
    body_text: str | None
    inline_images: list[dict]  # [{filename, cid, content_type, data}]
    attachments: list[dict]  # [{filename, content_type, size, data}]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _decode_mime_header(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        decoded = str(make_header(decode_header(value)))
    except Exception:
        decoded = value
    decoded = decoded.strip()
    return decoded or None


def _parse_addresses(value: str | None) -> tuple[str | None, str | None, list[tuple[str | None, str]]]:
    if not value:
        return None, None, []
    parsed = [(name.strip() or None, addr.strip()) for name, addr in getaddresses([value]) if addr.strip()]
    first_name = parsed[0][0] if parsed else None
    first_email = parsed[0][1] if parsed else None
    return first_name, first_email, parsed


def _extract_body(msg: Message) -> _ExtractedBody:
    html_parts: list[str] = []
    text_parts: list[str] = []
    inline_images: list[dict] = []
    attachments: list[dict] = []

    def _get_payload_content(part) -> bytes | None:
        try:
            return part.get_payload(decode=True)
        except Exception:
            return None

    def _decode_payload(payload: bytes | None, part) -> str:
        if payload is None:
            raw = part.get_payload()
            charset = part.get_content_charset() or "utf-8"
            if isinstance(raw, bytes):
                try:
                    return raw.decode(charset, errors="replace")
                except Exception:
                    return raw.decode("utf-8", errors="replace")
            return str(raw) if raw is not None else ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except Exception:
            return payload.decode("utf-8", errors="replace")

    def _decode_filename(param: str | None) -> str | None:
        if not param:
            return None
        try:
            decoded = str(make_header(decode_header(param)))
            return decoded.strip() or None
        except Exception:
            return param

    def _walk_part(part):
        disposition = (part.get("Content-Disposition") or "").lower()
        content_type = part.get_content_type()
        payload = _get_payload_content(part)
        cid = part.get("Content-ID")
        filename = _decode_filename(part.get("Content-Disposition")) or _decode_filename(part.get("Filename"))

        if "attachment" in disposition:
            if payload is not None and len(payload) <= MAX_ATTACHMENT_SIZE:
                attachments.append(
                    {
                        "filename": filename or "attachment",
                        "content_type": content_type,
                        "size": len(payload),
                        "data": payload,
                    }
                )
            return

        if content_type.startswith("image/") and cid:
            if payload is not None and len(payload) <= MAX_INLINE_IMAGE_SIZE:
                inline_images.append(
                    {
                        "filename": filename or f"image_{uuid.uuid4().hex[:8]}",
                        "cid": cid.strip("<>"),
                        "content_type": content_type,
                        "data": payload,
                    }
                )
            return

        if part.is_multipart():
            for subpart in part.walk():
                if subpart is part:
                    continue
                _walk_part(subpart)
        else:
            if content_type == "text/html":
                html_parts.append(_decode_payload(payload, part))
            elif content_type == "text/plain":
                text_parts.append(_decode_payload(payload, part))

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            _walk_part(part)
    else:
        payload = _get_payload_content(msg)
        if msg.get_content_type() == "text/html":
            html_parts.append(_decode_payload(payload, msg))
        else:
            text_parts.append(_decode_payload(payload, msg))

    body_html = "\n".join(p for p in html_parts if p).strip() or None
    body_text = "\n".join(p for p in text_parts if p).strip() or None
    if body_html and not body_text:
        body_text = body_html

    return _ExtractedBody(
        body_html=body_html,
        body_text=body_text,
        inline_images=inline_images,
        attachments=attachments,
    )


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def ensure_system_folders(db: AsyncSession, user_id: int) -> None:
    """Create default system folders for a user if they don't exist."""
    existing = await db.execute(
        select(MailFolder.name).where(
            MailFolder.user_id == user_id,
            MailFolder.name.in_(SYSTEM_FOLDERS),
        )
    )
    existing_names = set(existing.scalars().all())

    for name in SYSTEM_FOLDERS:
        if name not in existing_names:
            db.add(MailFolder(user_id=user_id, name=name, is_system=True))

    await db.commit()


async def get_or_create_folder(
    db: AsyncSession, user_id: int, name: str, is_system: bool = False
) -> MailFolder:
    """Get an existing folder or create a new one."""
    result = await db.execute(
        select(MailFolder).where(MailFolder.user_id == user_id, MailFolder.name == name)
    )
    folder = result.scalar_one_or_none()
    if folder is not None:
        return folder

    folder = MailFolder(user_id=user_id, name=name, is_system=is_system)
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


# --- Folder CRUD ---


async def list_folders(db: AsyncSession, user_id: int) -> list[MailFolder]:
    """List all folders for a user, system folders first."""
    result = await db.execute(
        select(MailFolder)
        .where(MailFolder.user_id == user_id)
        .order_by(MailFolder.is_system.desc(), MailFolder.name.asc())
    )
    return list(result.scalars().all())


async def create_folder(db: AsyncSession, user_id: int, payload: FolderCreate) -> MailFolder:
    """Create a new user folder."""
    name = payload.name.strip()
    existing = await db.execute(
        select(MailFolder).where(MailFolder.user_id == user_id, MailFolder.name == name)
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError(f"Folder '{name}' already exists")

    folder = MailFolder(user_id=user_id, name=name, is_system=False)
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


async def update_folder(db: AsyncSession, user_id: int, folder_id: int, name: str) -> MailFolder | None:
    """Rename a user folder."""
    result = await db.execute(
        select(MailFolder).where(
            MailFolder.id == folder_id,
            MailFolder.user_id == user_id,
            MailFolder.is_system.is_(False),
        )
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        return None

    folder.name = name.strip()
    await db.commit()
    await db.refresh(folder)
    return folder


async def delete_folder(db: AsyncSession, user_id: int, folder_id: int) -> bool:
    """Delete a user folder (messages are kept, folder_id becomes NULL)."""
    result = await db.execute(
        select(MailFolder).where(
            MailFolder.id == folder_id,
            MailFolder.user_id == user_id,
            MailFolder.is_system.is_(False),
        )
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        return False

    await db.delete(folder)
    await db.commit()
    return True


# --- Message CRUD ---


async def list_messages(
    db: AsyncSession,
    user_id: int,
    *,
    folder_id: int | None = None,
    folder_name: str | None = None,
    starred: bool | None = None,
    read: bool | None = None,
    q: str | None = None,
    account_email: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[MailMessage]:
    """List messages with optional filters."""
    query = select(MailMessage).where(MailMessage.user_id == user_id)

    if folder_id is not None:
        query = query.where(MailMessage.folder_id == folder_id)
    if folder_name is not None:
        query = query.join(MailFolder, MailMessage.folder_id == MailFolder.id).where(
            MailFolder.user_id == user_id,
            MailFolder.name == folder_name,
        )
    if starred is not None:
        query = query.where(MailMessage.is_starred.is_(starred))
    if read is not None:
        query = query.where(MailMessage.is_read.is_(read))

    if account_email:
        email_value = _normalize_email(account_email)
        pattern = f"%{email_value}%"
        query = query.where(
            (func.lower(func.coalesce(MailMessage.sender_email, "")).like(pattern))
            | (func.lower(func.coalesce(MailMessage.recipient_emails, "")).like(pattern))
        )

    if q:
        term = f"%{q.strip().lower()}%"
        query = query.where(
            (func.lower(func.coalesce(MailMessage.subject, "")).like(term))
            | (func.lower(func.coalesce(MailMessage.sender_name, "")).like(term))
            | (func.lower(func.coalesce(MailMessage.sender_email, "")).like(term))
            | (func.lower(func.coalesce(MailMessage.recipient_emails, "")).like(term))
            | (func.lower(func.coalesce(MailMessage.body_text, "")).like(term))
        )

    query = (
        query.order_by(
            MailMessage.sent_at.desc().nullslast(),
            MailMessage.created_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )

    result = await db.execute(query)
    return list(result.scalars().unique().all())


async def get_message(db: AsyncSession, user_id: int, message_id: int) -> MailMessage | None:
    """Get a single message by id."""
    result = await db.execute(
        select(MailMessage)
        .where(MailMessage.id == message_id, MailMessage.user_id == user_id)
        .options(selectinload(MailMessage.folder))
    )
    return result.scalar_one_or_none()


async def get_message_by_message_id(db: AsyncSession, user_id: int, message_id: str) -> MailMessage | None:
    """Get a message by its email Message-ID header."""
    result = await db.execute(
        select(MailMessage).where(
            MailMessage.user_id == user_id,
            MailMessage.message_id == message_id,
        )
    )
    return result.scalar_one_or_none()


async def create_message(
    db: AsyncSession,
    user_id: int,
    payload: MessageCreate,
    folder_id: int | None = None,
    *,
    sender_name: str | None = None,
    sender_email: str | None = None,
    recipient_names: str | None = None,
    is_read: bool = True,
    sent_at: datetime | None = None,
    received_at: datetime | None = None,
    message_id: str | None = None,
    thread_id: str | None = None,
    raw_source: str | None = None,
) -> MailMessage:
    """Store a new message."""
    now = _utcnow()
    to_values = [value.strip() for value in payload.to if value and value.strip()]
    msg = MailMessage(
        user_id=user_id,
        folder_id=folder_id,
        thread_id=thread_id or uuid.uuid4().hex[:16],
        message_id=message_id,
        subject=payload.subject,
        body_html=_sanitize_html(payload.body_html),
        body_text=payload.body_text,
        in_reply_to=payload.in_reply_to,
        sender_name=sender_name,
        sender_email=sender_email,
        recipient_names=recipient_names,
        recipient_emails=", ".join(to_values),
        sent_at=sent_at or now,
        received_at=received_at,
        raw_source=raw_source,
        created_at=now,
        updated_at=now,
        is_read=is_read,
    )
    db.add(msg)
    await db.commit()
    await db.refresh(msg)
    await extract_contacts_from_message(db, user_id, msg)
    return msg


async def update_message(db: AsyncSession, message: MailMessage, payload: MessageUpdate) -> MailMessage:
    """Update message fields."""
    if payload.subject is not None:
        message.subject = payload.subject
    if payload.body_html is not None:
        message.body_html = _sanitize_html(payload.body_html)
    if payload.body_text is not None:
        message.body_text = payload.body_text
    if payload.is_starred is not None:
        message.is_starred = payload.is_starred
    if payload.is_read is not None:
        message.is_read = payload.is_read
    if payload.folder_id is not None:
        message.folder_id = payload.folder_id
    message.updated_at = _utcnow()
    await db.commit()
    await db.refresh(message)
    return message


async def delete_message(db: AsyncSession, message: MailMessage) -> None:
    """Delete a message."""
    await db.delete(message)
    await db.commit()


async def mark_as_read(db: AsyncSession, user_id: int, message_id: int) -> MailMessage | None:
    """Mark a message as read."""
    message = await get_message(db, user_id, message_id)
    if message is None:
        return None
    if not message.is_read:
        message.is_read = True
        message.updated_at = _utcnow()
        await db.commit()
        await db.refresh(message)
    return message


async def get_thread(db: AsyncSession, user_id: int, thread_id: str, limit: int = 100) -> list[MailMessage]:
    """Get all messages in a thread."""
    result = await db.execute(
        select(MailMessage)
        .where(MailMessage.user_id == user_id, MailMessage.thread_id == thread_id)
        .order_by(MailMessage.sent_at.asc().nullsfirst(), MailMessage.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_thread_summary(db: AsyncSession, user_id: int, message_id: int) -> dict | None:
    """Get thread summary for a message."""
    message = await get_message(db, user_id, message_id)
    if message is None or message.thread_id is None:
        return None

    messages = await get_thread(db, user_id, message.thread_id)
    if not messages:
        return None

    unread_count = sum(1 for m in messages if not m.is_read)
    last_at = max((m.sent_at or m.created_at) for m in messages)
    return {
        "thread_id": message.thread_id,
        "subject": message.subject or "(no subject)",
        "message_count": len(messages),
        "unread_count": unread_count,
        "last_at": last_at,
    }


# --- Contact CRUD ---


async def list_contacts(db: AsyncSession, user_id: int) -> list[MailContact]:
    """List all contacts for a user."""
    result = await db.execute(
        select(MailContact)
        .where(MailContact.user_id == user_id)
        .order_by(func.coalesce(MailContact.name, MailContact.email).asc())
    )
    return list(result.scalars().all())


async def create_contact(db: AsyncSession, user_id: int, email: str, name: str | None = None) -> MailContact:
    """Create or update a contact."""
    normalized = _normalize_email(email)
    result = await db.execute(
        select(MailContact).where(
            MailContact.user_id == user_id,
            MailContact.email == normalized,
        )
    )
    contact = result.scalar_one_or_none()
    if contact is not None:
        if name is not None and name.strip():
            contact.name = name.strip()
        contact.is_trusted = True
        await db.commit()
        await db.refresh(contact)
        return contact

    contact = MailContact(
        user_id=user_id,
        email=normalized,
        name=name.strip() if name and name.strip() else None,
        is_trusted=True,
    )
    db.add(contact)
    await db.commit()
    await db.refresh(contact)
    return contact


async def delete_contact(db: AsyncSession, user_id: int, contact_id: int) -> bool:
    """Delete a contact."""
    result = await db.execute(
        select(MailContact).where(MailContact.id == contact_id, MailContact.user_id == user_id)
    )
    contact = result.scalar_one_or_none()
    if contact is None:
        return False
    await db.delete(contact)
    await db.commit()
    return True


async def find_contact_by_email(db: AsyncSession, user_id: int, email: str) -> MailContact | None:
    """Find a contact by email."""
    result = await db.execute(
        select(MailContact).where(
            MailContact.user_id == user_id,
            MailContact.email == _normalize_email(email),
        )
    )
    return result.scalar_one_or_none()


async def extract_contacts_from_message(db: AsyncSession, user_id: int, message: MailMessage) -> None:
    """Auto-extract contacts from a message's sender and recipients."""
    if message.sender_email:
        await create_contact(db, user_id, message.sender_email, message.sender_name)
    if message.recipient_emails:
        for raw in message.recipient_emails.split(","):
            raw = raw.strip()
            if not raw:
                continue
            _, email_addr, parsed = _parse_addresses(raw)
            if parsed:
                for name, addr in parsed:
                    await create_contact(db, user_id, addr, name)
            elif email_addr:
                await create_contact(db, user_id, email_addr)


# --- Attachment CRUD ---


async def list_message_attachments(db: AsyncSession, user_id: int, message_id: int) -> list[MailAttachment]:
    """List all attachments for a message."""
    result = await db.execute(
        select(MailAttachment)
        .where(
            MailAttachment.user_id == user_id,
            MailAttachment.message_id == message_id,
        )
        .order_by(MailAttachment.is_inline.asc(), MailAttachment.filename.asc())
    )
    return list(result.scalars().all())


async def get_attachment(db: AsyncSession, user_id: int, attachment_id: int) -> MailAttachment | None:
    """Get a single attachment by id."""
    result = await db.execute(
        select(MailAttachment).where(
            MailAttachment.id == attachment_id,
            MailAttachment.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def store_attachments(
    db: AsyncSession,
    user_id: int,
    message_id: int,
    items: list[dict],
    is_inline: bool = False,
) -> list[MailAttachment]:
    """Store attachment data and return persisted models."""
    stored: list[MailAttachment] = []
    for item in items:
        data = item.get("data")
        if data is None:
            continue
        attachment = MailAttachment(
            user_id=user_id,
            message_id=message_id,
            filename=item["filename"],
            content_type=item.get("content_type", "application/octet-stream"),
            size=len(data),
            is_inline=is_inline,
            cid=item.get("cid"),
            data=data,
        )
        db.add(attachment)
        stored.append(attachment)
    if stored:
        await db.commit()
        for a in stored:
            await db.refresh(a)
    return stored


def _replace_inline_cid(html: str | None, cid_map: dict[str, str]) -> str | None:
    """Replace cid:... references in HTML with data: URIs for stored images."""
    if not html or not cid_map:
        return html
    pattern = r"cid:([A-Za-z0-9][A-Za-z0-9!#$%&'*+/=?^_`{|}~-]*)"
    return re.sub(pattern, lambda m: cid_map.get(m.group(1), m.group(0)), html)


async def replace_inline_cid_in_message(
    db: AsyncSession, user_id: int, message_id: int
) -> MailMessage | None:
    """Replace cid references in body_html with data URIs based on stored inline images."""
    message = await get_message(db, user_id, message_id)
    if message is None or not message.body_html:
        return message

    result = await db.execute(
        select(MailAttachment).where(
            MailAttachment.user_id == user_id,
            MailAttachment.message_id == message_id,
            MailAttachment.is_inline.is_(True),
        )
    )
    inline_images = result.scalars().all()
    cid_map: dict[str, str] = {}
    for img in inline_images:
        if img.data and img.cid:
            b64 = base64.b64encode(img.data).decode("ascii")
            cid_map[img.cid] = f"data:{img.content_type};base64,{b64}"

    if cid_map:
        message.body_html = _replace_inline_cid(message.body_html, cid_map)
        message.updated_at = _utcnow()
        await db.commit()
        await db.refresh(message)
    return message


# --- Account CRUD ---


async def list_accounts(db: AsyncSession, user_id: int) -> list[MailAccount]:
    result = await db.execute(
        select(MailAccount)
        .where(MailAccount.user_id == user_id)
        .order_by(MailAccount.is_active.desc(), MailAccount.created_at.asc())
    )
    return list(result.scalars().all())


async def get_account(db: AsyncSession, user_id: int, account_id: int) -> MailAccount | None:
    result = await db.execute(
        select(MailAccount).where(MailAccount.id == account_id, MailAccount.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def create_account(db: AsyncSession, user_id: int, payload: MailAccountCreate) -> MailAccount:
    now = _utcnow()
    account = MailAccount(
        user_id=user_id,
        name=(payload.name or "").strip() or None,
        email_address=_normalize_email(payload.email_address),
        imap_host=payload.imap_host.strip(),
        imap_port=int(payload.imap_port),
        imap_username=payload.imap_username.strip(),
        imap_password=encrypt_secret(payload.imap_password),
        imap_use_ssl=bool(payload.imap_use_ssl),
        smtp_host=payload.smtp_host.strip(),
        smtp_port=int(payload.smtp_port),
        smtp_username=payload.smtp_username.strip(),
        smtp_password=encrypt_secret(payload.smtp_password),
        smtp_use_tls=bool(payload.smtp_use_tls),
        is_active=bool(payload.is_active),
        created_at=now,
        updated_at=now,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    return account


async def update_account(db: AsyncSession, account: MailAccount, payload: MailAccountUpdate) -> MailAccount:
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if field == "email_address":
                value = _normalize_email(value)
            # Encrypt password fields before storing
            if field in ("imap_password", "smtp_password") and value:
                value = encrypt_secret(value)
        setattr(account, field, value)
    account.updated_at = _utcnow()
    await db.commit()
    await db.refresh(account)
    return account


async def delete_account(db: AsyncSession, account: MailAccount) -> None:
    await db.delete(account)
    await db.commit()


# --- SMTP sending ---


def _smtp_configured(settings) -> bool:
    return bool(getattr(settings, "MAIL_SMTP_HOST", "") and getattr(settings, "MAIL_SMTP_USERNAME", ""))


def _send_via_smtp(
    payload: MessageCreate,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    from_email: str | None = None,
    from_name: str | None = None,
) -> None:
    sender = from_email or username
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{sender}>" if from_name else sender
    msg["To"] = ", ".join(payload.to)
    msg["Subject"] = payload.subject

    plain = payload.body_text or ""
    if payload.body_html:
        if plain:
            msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(payload.body_html, "html", "utf-8"))
    else:
        msg.attach(MIMEText(plain, "plain", "utf-8"))

    if use_tls and int(port) == 465:
        server = smtplib.SMTP_SSL(host, int(port), timeout=30)
        try:
            server.ehlo()
            if username and password:
                server.login(username, password)
            server.send_message(msg)
        finally:
            server.quit()
        return

    server = smtplib.SMTP(host, int(port), timeout=30)
    try:
        server.ehlo()
        if use_tls:
            server.starttls()
            server.ehlo()
        if username and password:
            server.login(username, password)
        server.send_message(msg)
    finally:
        server.quit()


async def send_email(
    payload: MessageCreate,
    *,
    from_email: str | None = None,
    account: MailAccount | None = None,
) -> None:
    """Send an email via configured account SMTP or global SMTP settings."""
    if account is not None:
        _, smtp_pw = _decrypt_account_passwords(account)
        _send_via_smtp(
            payload,
            host=account.smtp_host,
            port=account.smtp_port,
            username=account.smtp_username,
            password=smtp_pw,
            use_tls=account.smtp_use_tls,
            from_email=from_email or account.email_address,
            from_name=account.name,
        )
        return

    settings = get_settings()
    if not _smtp_configured(settings):
        raise RuntimeError("SMTP not configured. Set MAIL_SMTP_HOST and MAIL_SMTP_USERNAME.")

    _send_via_smtp(
        payload,
        host=settings.MAIL_SMTP_HOST,
        port=int(settings.MAIL_SMTP_PORT),
        username=settings.MAIL_SMTP_USERNAME,
        password=settings.MAIL_SMTP_PASSWORD,
        use_tls=bool(getattr(settings, "MAIL_SMTP_USE_TLS", True)),
        from_email=from_email or settings.MAIL_SMTP_USERNAME,
        from_name=getattr(settings, "MAIL_SENDER_NAME", "") or None,
    )


# --- IMAP sync ---


def _decrypt_account_passwords(account: MailAccount) -> tuple[str, str]:
    """Return (imap_password, smtp_password) decrypted from the account."""
    try:
        imap_pw = decrypt_or_raise(account.imap_password, "imap_password")
    except ValueError:
        imap_pw = ""
    try:
        smtp_pw = decrypt_or_raise(account.smtp_password, "smtp_password")
    except ValueError:
        smtp_pw = ""
    return imap_pw, smtp_pw


def _imap_fetch_messages(
    account: MailAccount,
    limit: int = 30,
    search_criteria: str = "UNSEEN",
) -> list[Message]:
    """Fetch messages from IMAP inbox.

    *search_criteria* follows the IMAP SEARCH command syntax.
    ``"UNSEEN"`` (default) pulls only unread messages — fast for
    accounts with thousands of messages.  ``"ALL"`` re-syncs
    everything, which is useful on first connection or manual
    full-sync.
    """
    imap_pw, _ = _decrypt_account_passwords(account)
    if account.imap_use_ssl:
        client = imaplib.IMAP4_SSL(account.imap_host, int(account.imap_port))
    else:
        client = imaplib.IMAP4(account.imap_host, int(account.imap_port))
    try:
        client.login(account.imap_username, imap_pw)
        typ, _ = client.select("INBOX")
        if typ != "OK":
            raise RuntimeError("Не удалось открыть INBOX")
        typ, data = client.search(None, search_criteria)
        if typ != "OK":
            return []
        ids = (data[0] or b"").split()
        if not ids:
            return []
        messages: list[Message] = []
        for msg_id in ids[-limit:]:
            typ, msg_data = client.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not msg_data:
                continue
            raw = None
            for part in msg_data:
                if isinstance(part, tuple) and part[1]:
                    raw = part[1]
                    break
            if raw is None:
                continue
            try:
                parsed = email.message_from_bytes(raw)
            except Exception:
                continue
            messages.append(parsed)
        return messages
    finally:
        try:
            client.logout()
        except Exception:
            try:
                client.shutdown()
            except Exception:
                pass


async def sync_account_inbox(
    db: AsyncSession,
    user_id: int,
    account: MailAccount,
    limit: int = 30,
    *,
    sync_mode: str = "recent",
) -> dict:
    """Fetch messages from IMAP inbox into local inbox folder.

    *sync_mode* controls which messages are fetched:

    * ``"recent"`` (default) — fetches ``UNSEEN`` messages only.
    * ``"all"`` — fetches ``ALL`` messages (full re-sync).
    """
    search_criteria = "ALL" if sync_mode == "all" else "UNSEEN"
    await ensure_system_folders(db, user_id)
    inbox = await get_or_create_folder(db, user_id, "inbox", is_system=True)

    try:
        fetched = _imap_fetch_messages(account, limit=limit, search_criteria=search_criteria)
    except Exception as exc:
        raise RuntimeError(f"IMAP sync failed: {exc}") from exc

    created = 0
    skipped = 0
    for msg in fetched:
        msg_id_header = _decode_mime_header(msg.get("Message-ID"))
        if msg_id_header:
            existing = await get_message_by_message_id(db, user_id, msg_id_header)
            if existing is not None:
                skipped += 1
                continue

        subject = _decode_mime_header(msg.get("Subject")) or "(no subject)"
        sender_name, sender_email, _ = _parse_addresses(_decode_mime_header(msg.get("From")))
        _, _, to_parsed = _parse_addresses(_decode_mime_header(msg.get("To")))
        recipient_emails = ", ".join(addr for _, addr in to_parsed) if to_parsed else None
        recipient_names = ", ".join(name for name, _ in to_parsed if name) if to_parsed else None
        extracted = _extract_body(msg)
        body_html = _sanitize_html(extracted.body_html)
        body_text = extracted.body_text
        sent_at = _parse_date(msg.get("Date"))
        in_reply_to = _decode_mime_header(msg.get("In-Reply-To"))

        stored = MailMessage(
            user_id=user_id,
            folder_id=inbox.id,
            thread_id=uuid.uuid4().hex[:16],
            message_id=msg_id_header,
            in_reply_to=in_reply_to,
            subject=subject,
            sender_name=sender_name,
            sender_email=sender_email,
            recipient_names=recipient_names,
            recipient_emails=recipient_emails,
            body_html=body_html,
            body_text=body_text,
            has_attachments=len(extracted.attachments) > 0,
            is_read=False,
            sent_at=sent_at,
            received_at=_utcnow(),
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        db.add(stored)
        await db.flush()

        # Store inline images and attachments
        if extracted.inline_images:
            await store_attachments(db, user_id, stored.id, extracted.inline_images, is_inline=True)
        if extracted.attachments:
            await store_attachments(db, user_id, stored.id, extracted.attachments, is_inline=False)

        await extract_contacts_from_message(db, user_id, stored)
        created += 1

    account.last_synced_at = _utcnow()
    account.updated_at = _utcnow()
    await db.commit()
    return {"fetched": len(fetched), "created": created, "skipped": skipped}


# --- Statistics ---


async def get_folder_counts(db: AsyncSession, user_id: int) -> list[dict]:
    """Get folder list with unread counts."""
    folders = await list_folders(db, user_id)
    counts_result = await db.execute(
        select(
            MailMessage.folder_id,
            func.count(MailMessage.id).label("total"),
            func.coalesce(
                func.sum(case((MailMessage.is_read.is_(False), 1), else_=0)),
                0,
            ).label("unread"),
        )
        .where(MailMessage.user_id == user_id)
        .group_by(MailMessage.folder_id)
    )

    counts: dict[int | None, dict[str, int]] = {}
    for folder_id, total, unread in counts_result.all():
        counts[folder_id] = {"total": int(total or 0), "unread": int(unread or 0)}

    return [
        {
            "id": f.id,
            "name": f.name,
            "is_system": f.is_system,
            "total": counts.get(f.id, {}).get("total", 0),
            "unread": counts.get(f.id, {}).get("unread", 0),
        }
        for f in folders
    ]
