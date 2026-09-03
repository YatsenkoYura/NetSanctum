"""HTTP boundary for manifest-declared module integrations."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationUnavailableError,
)
from app.core.modules import module_registry
from app.core.security import get_current_user

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


@router.get("")
async def list_integrations(user=Depends(get_current_user)):
    """Expose active integration contracts and their JSON schemas."""
    return module_registry.integration_catalog()


@router.get("/contracts")
async def list_integration_contracts(user=Depends(get_current_user)):
    """Expose shared contracts and every active provider implementing them."""
    return module_registry.integration_contract_catalog()


@router.get("/ui-actions")
async def list_ui_actions(
    request: Request,
    slot: str,
    entity_type: str,
    entity_id: str,
    user=Depends(get_current_user),
):
    """Resolve actions for a framework UI slot and entity context."""
    lang = request.cookies.get("lang", "en")
    return module_registry.ui_actions(
        slot,
        {"entity_type": entity_type, "entity_id": entity_id},
        lang,
    )


@router.post("/{integration_id}")
async def invoke_integration(
    integration_id: str,
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Invoke a versioned integration through the shared module registry."""
    try:
        return await module_registry.invoke_integration(
            integration_id,
            payload,
            IntegrationContext(session=db, user=user, registry=module_registry),
        )
    except IntegrationUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntegrationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc
    except IntegrationRejectedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
