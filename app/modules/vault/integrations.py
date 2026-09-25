from sqlalchemy import select

from app.contracts.undo_v1 import UndoRequest, UndoResult
from app.contracts.vault_capture_v1 import VaultCaptureRequest, VaultCaptureResult
from app.core.module_types import IntegrationContext
from app.modules.vault.models import VaultItem
from app.modules.vault.schemas import VaultItemCreate
from app.modules.vault.services import create_vault_item


async def capture_item(
    request: VaultCaptureRequest,
    context: IntegrationContext,
) -> VaultCaptureResult:
    item = await create_vault_item(
        context.session,
        VaultItemCreate(
            entry_type="bookmark" if request.kind == "bookmark" else "thought",
            node_type="bookmark" if request.kind == "bookmark" else "note",
            title=request.title,
            content=request.content,
            url=str(request.url) if request.url else None,
            score=None,
            status=None,
            category=None,
            auto_fetch_og=False,
        ),
    )
    return VaultCaptureResult(
        item_id=item.id,
        kind=request.kind,
        title=item.title,
        message=f"Saved {request.kind} to Vault",
    )


async def undo_capture(
    request: UndoRequest,
    context: IntegrationContext,
) -> UndoResult:
    """Remove the Vault entry a capture created, addressed by what it captured.

    The original arguments are the identity of the item: a bookmark is found by its
    URL, a note by its title. A capture that stored neither cannot be undone blindly.
    """
    url = str(request.arguments.get("url") or "").strip()
    title = str(request.arguments.get("title") or "").strip()[:200]
    if not url and not title:
        return UndoResult(status="not_addressable", detail="The capture had no url or title")
    statement = select(VaultItem).where(VaultItem.user_id == context.user.id)
    statement = statement.where(VaultItem.url == url) if url else statement.where(VaultItem.title == title)
    item = (await context.session.scalars(statement.limit(1))).first()
    if item is None:
        return UndoResult(status="missing", detail="The entry is already gone")
    await context.session.delete(item)
    await context.session.flush()
    return UndoResult(status="undone", detail=f"Removed {item.title[:120]}")
