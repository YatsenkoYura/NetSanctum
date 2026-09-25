from sqlalchemy import select

from app.contracts.search_documents_v1 import (
    SearchDocument,
    SearchDocumentsRequest,
    SearchDocumentsResult,
)
from app.core.module_types import IntegrationContext
from app.modules.vault.models import VaultItem


async def search_documents(
    request: SearchDocumentsRequest,
    context: IntegrationContext,
) -> SearchDocumentsResult:
    result = await context.session.execute(
        select(VaultItem)
        .where(VaultItem.is_archived.is_(False), VaultItem.is_folder.is_(False))
        .order_by(VaultItem.updated_at.desc(), VaultItem.id.desc())
        .offset(request.offset)
        .limit(request.limit + 1)
    )
    items = list(result.scalars())
    return SearchDocumentsResult(
        module_id="vault",
        documents=[
            SearchDocument(
                document_id=str(item.id),
                entity_type=item.node_type or item.entry_type or "note",
                title=(item.title or item.og_title or f"Vault item #{item.id}")[:255],
                subtitle=(item.category or item.entry_type or "")[:255] or None,
                body=(item.content or item.og_description or "")[:4000] or None,
                keywords=[str(tag)[:100] for tag in (item.tags or []) if str(tag).strip()][:50],
                updated_at=item.updated_at or item.created_at,
                open_path="/vault/dashboard",
            )
            for item in items[: request.limit]
        ],
        next_offset=request.offset + request.limit if len(items) > request.limit else None,
    )
