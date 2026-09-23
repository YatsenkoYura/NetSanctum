from app.contracts.vault_capture_v1 import VaultCaptureRequest, VaultCaptureResult
from app.core.module_types import IntegrationContext
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
