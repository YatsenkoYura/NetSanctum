from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.module_types import IntegrationContext
from app.core.modules import module_registry
from app.core.security import get_current_user
from app.modules.search.integrations import refresh_index, refresh_lock
from app.modules.search.models import SearchSyncState

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/status")
async def search_status(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    states = list(
        (await db.scalars(select(SearchSyncState).order_by(SearchSyncState.source_module_id))).all()
    )
    return {
        "providers": [
            {
                "module_id": state.source_module_id,
                "integration_id": state.source_integration_id,
                "document_count": state.document_count,
                "last_completed_at": state.last_completed_at,
                "stale": state.last_error is not None,
            }
            for state in states
        ]
    }


@router.post("/reindex")
async def reindex_search(
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    async with refresh_lock:
        warnings = await refresh_index(
            IntegrationContext(
                session=db,
                user=user,
                registry=module_registry,
                consumer_id="search",
            ),
            force=True,
        )
    states = list(await db.scalars(select(SearchSyncState)))
    return {
        "status": "completed",
        "document_count": sum(state.document_count for state in states),
        "warnings": warnings,
    }
