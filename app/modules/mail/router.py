"""FastAPI routes for the Mail module."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.core.templates import templates
from app.modules.mail import services
from app.modules.mail.schemas import (
    AttachmentResponse,
    ContactCreate,
    ContactResponse,
    FolderCreate,
    FolderResponse,
    MailAccountCreate,
    MailAccountResponse,
    MailAccountUpdate,
    MessageCreate,
    MessageResponse,
    MessageUpdate,
    SmtpConfigResponse,
    ThreadResponse,
)

router = APIRouter()


@router.get("/mail", response_class=HTMLResponse, include_in_schema=False)
async def mail_dashboard(
    request: Request,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await services.ensure_system_folders(db, user.id)
    lang = request.cookies.get("lang", "ru")
    return templates.TemplateResponse(
        request,
        "mail_dashboard.html",
        {"user": user, "lang": lang},
    )


@router.get("/mail/accounts/new", response_class=HTMLResponse, include_in_schema=False)
async def mail_account_connect_page(
    request: Request,
    user=Depends(get_current_user),
):
    lang = request.cookies.get("lang", "ru")
    return templates.TemplateResponse(
        request,
        "mail_account_connect.html",
        {"user": user, "lang": lang},
    )


# --- Folder routes ---


@router.get("/api/mail/folders", response_model=list[FolderResponse])
async def api_list_folders(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await services.ensure_system_folders(db, user.id)
    return await services.list_folders(db, user.id)


@router.post("/api/mail/folders", response_model=FolderResponse, status_code=201)
async def api_create_folder(
    payload: FolderCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await services.create_folder(db, user.id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/api/mail/folders/{folder_id}")
async def api_delete_folder(
    folder_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await services.delete_folder(db, user.id, folder_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Folder not found")
    return {"status": "deleted", "id": folder_id}


# --- Attachment routes ---


@router.get("/api/mail/messages/{message_id}/attachments", response_model=list[AttachmentResponse])
async def api_list_attachments(
    message_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await services.get_message(db, user.id, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return await services.list_message_attachments(db, user.id, message_id)


@router.get("/api/mail/attachments/{attachment_id}")
async def api_download_attachment(
    attachment_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    attachment = await services.get_attachment(db, user.id, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if attachment.data is None:
        raise HTTPException(status_code=404, detail="Attachment data not available")
    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"',
        },
    )


@router.get("/api/mail/attachments/{attachment_id}/preview")
async def api_preview_attachment(
    attachment_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Preview inline image or any attachment as inline content."""
    attachment = await services.get_attachment(db, user.id, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if attachment.data is None:
        raise HTTPException(status_code=404, detail="Attachment data not available")
    return Response(
        content=attachment.data,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{attachment.filename}"',
        },
    )


# --- Message routes ---


@router.get("/api/mail/messages", response_model=list[MessageResponse])
async def api_list_messages(
    folder_id: int | None = Query(None),
    folder_name: str | None = Query(None),
    starred: bool | None = Query(None),
    unread_only: bool = Query(False),
    q: str | None = Query(None),
    account_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    read = False if unread_only else None
    account_email = None
    if account_id is not None:
        account = await services.get_account(db, user.id, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")
        account_email = account.email_address
    return await services.list_messages(
        db,
        user.id,
        folder_id=folder_id,
        folder_name=folder_name,
        starred=starred,
        read=read,
        q=q,
        account_email=account_email,
        limit=limit,
        offset=offset,
    )


@router.get("/api/mail/messages/{message_id}", response_model=MessageResponse)
async def api_get_message(
    message_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await services.get_message(db, user.id, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    # Replace CID references with data URIs for inline images
    message = await services.replace_inline_cid_in_message(db, user.id, message_id)
    return message


@router.patch("/api/mail/messages/{message_id}", response_model=MessageResponse)
async def api_update_message(
    message_id: int,
    payload: MessageUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await services.get_message(db, user.id, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return await services.update_message(db, message, payload)


@router.delete("/api/mail/messages/{message_id}")
async def api_delete_message(
    message_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await services.get_message(db, user.id, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    await services.delete_message(db, message)
    return {"status": "deleted", "id": message_id}


@router.post("/api/mail/messages/{message_id}/read")
async def api_mark_as_read(
    message_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    message = await services.mark_as_read(db, user.id, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return {"status": "read", "id": message_id}


@router.post("/api/mail/messages", response_model=MessageResponse, status_code=201)
async def api_create_message(
    payload: MessageCreate,
    account_id: int | None = Query(None),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await services.ensure_system_folders(db, user.id)
    account = None
    if account_id is not None:
        account = await services.get_account(db, user.id, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

    resolved_to: list[str] = []
    for to_email in payload.to:
        clean = to_email.strip()
        if not clean:
            continue
        contact = await services.find_contact_by_email(db, user.id, clean)
        if contact and contact.name:
            resolved_to.append(f"{contact.name} <{contact.email}>")
        else:
            resolved_to.append(clean)

    if not resolved_to:
        raise HTTPException(status_code=422, detail="At least one recipient is required")

    msg = MessageCreate(
        to=resolved_to,
        subject=payload.subject,
        body_html=payload.body_html,
        body_text=payload.body_text,
        in_reply_to=payload.in_reply_to,
    )
    sent_folder = await services.get_or_create_folder(db, user.id, "sent", is_system=True)
    settings = get_settings()
    sender_email = (account.email_address if account else getattr(settings, "MAIL_SMTP_USERNAME", "")) or None
    sender_name = (account.name if account else getattr(settings, "MAIL_SENDER_NAME", "")) or None
    message = await services.create_message(
        db,
        user.id,
        msg,
        folder_id=sent_folder.id,
        sender_email=sender_email,
        sender_name=sender_name,
        is_read=True,
    )

    try:
        await services.send_email(msg, account=account)
    except Exception:
        # Keep stored copy even if delivery failed (e.g. SMTP not configured/network/auth).
        pass

    return message


@router.get("/api/mail/threads/{thread_id}", response_model=ThreadResponse)
async def api_get_thread(
    thread_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    messages = await services.get_thread(db, user.id, thread_id)
    if not messages:
        raise HTTPException(status_code=404, detail="Thread not found")

    unread_count = sum(1 for m in messages if not m.is_read)
    last_at = max((m.sent_at or m.created_at) for m in messages)
    return ThreadResponse(
        thread_id=thread_id,
        subject=messages[0].subject or "(no subject)",
        messages=[MessageResponse.model_validate(m) for m in messages],
        last_at=last_at,
        unread_count=unread_count,
    )


# --- Account routes ---


@router.get("/api/mail/accounts", response_model=list[MailAccountResponse])
async def api_list_accounts(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await services.list_accounts(db, user.id)


@router.post("/api/mail/accounts", response_model=MailAccountResponse, status_code=201)
async def api_create_account(
    payload: MailAccountCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await services.create_account(db, user.id, payload)


@router.patch("/api/mail/accounts/{account_id}", response_model=MailAccountResponse)
async def api_update_account(
    account_id: int,
    payload: MailAccountUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await services.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return await services.update_account(db, account, payload)


@router.delete("/api/mail/accounts/{account_id}")
async def api_delete_account(
    account_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await services.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    await services.delete_account(db, account)
    return {"status": "deleted", "id": account_id}


@router.post("/api/mail/accounts/{account_id}/sync")
async def api_sync_account(
    account_id: int,
    limit: int = Query(30, ge=1, le=100),
    sync_mode: str = Query("recent", regex="^(recent|all)$"),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    account = await services.get_account(db, user.id, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        result = await services.sync_account_inbox(db, user.id, account, limit=limit, sync_mode=sync_mode)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "ok", **result}


# --- Contact routes ---


@router.get("/api/mail/contacts", response_model=list[ContactResponse])
async def api_list_contacts(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await services.list_contacts(db, user.id)


@router.post("/api/mail/contacts", response_model=ContactResponse, status_code=201)
async def api_create_contact(
    payload: ContactCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await services.create_contact(db, user.id, payload.email, payload.name)


@router.delete("/api/mail/contacts/{contact_id}")
async def api_delete_contact(
    contact_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    deleted = await services.delete_contact(db, user.id, contact_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"status": "deleted", "id": contact_id}


# --- SMTP config ---


@router.get("/api/mail/smtp", response_model=SmtpConfigResponse)
async def api_get_smtp_config(user=Depends(get_current_user)):
    settings = get_settings()
    return SmtpConfigResponse(
        host=getattr(settings, "MAIL_SMTP_HOST", ""),
        port=int(getattr(settings, "MAIL_SMTP_PORT", 587)),
        username=getattr(settings, "MAIL_SMTP_USERNAME", ""),
        use_tls=bool(getattr(settings, "MAIL_SMTP_USE_TLS", True)),
    )


# --- Stats ---


@router.get("/api/mail/stats/folders")
async def api_folder_stats(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await services.ensure_system_folders(db, user.id)
    return await services.get_folder_counts(db, user.id)
