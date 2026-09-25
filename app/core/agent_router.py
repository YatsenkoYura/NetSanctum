"""Internal HTTP boundary between the isolated agent runtime and this process.

The agent runtime has no database access and no session cookies: it may only discover
declared integrations, call them, and read the content a provider already exposes.
Authentication is a dedicated shared key, deliberately separate from user credentials.
"""

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent.catalog import build_tool_catalog
from app.core.agent.primitives import READ_TEXT_LIMIT, AgentReadRequest
from app.core.config import get_settings
from app.core.database import get_db
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationResource,
    IntegrationServiceError,
    IntegrationUnavailableError,
)
from app.core.modules import module_registry
from app.core.security import OwnerUser

router = APIRouter(prefix="/internal/agent", tags=["agent"])

MAX_RESOURCE_TEXT = READ_TEXT_LIMIT
MAX_INVOKE_PARAMETERS_BYTES = 8_192
DEFAULT_CONSUMER = "miku"


class AgentInvokeRequest(BaseModel):
    integration_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]*\.v[1-9][0-9]*$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    consumer_id: str = Field(default=DEFAULT_CONSUMER, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")


class AgentInvokeResponse(BaseModel):
    result: dict[str, Any]


class AgentResourceRequest(AgentReadRequest):
    module_id: str = Field(min_length=1, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")
    item_id: str = Field(min_length=1, max_length=200)
    child_id: str | None = Field(default=None, max_length=200)
    page: int | None = Field(default=None, ge=0, le=5_000)
    consumer_id: str = Field(default=DEFAULT_CONSUMER, max_length=63, pattern=r"^[a-z][a-z0-9_]*$")


async def require_agent_key(x_agent_key: str = Header(default="")) -> OwnerUser:
    """Only the agent runtime, holding the shared key, may use these routes."""
    expected = get_settings().AGENT_INTERNAL_KEY
    if not expected or not hmac.compare_digest(x_agent_key, expected):
        raise HTTPException(status_code=403, detail="Invalid agent key")
    return OwnerUser()


def _context(db: AsyncSession, user, consumer_id: str) -> IntegrationContext:
    return IntegrationContext(session=db, user=user, registry=module_registry, consumer_id=consumer_id)


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip(), True


def _fail(exc: Exception) -> HTTPException:
    if isinstance(exc, (IntegrationNotFoundError, IntegrationUnavailableError)):
        return HTTPException(status_code=404, detail="The requested capability is unavailable")
    if isinstance(exc, (IntegrationRejectedError, ValidationError)):
        return HTTPException(status_code=422, detail="The request was rejected")
    return HTTPException(status_code=503, detail="The requested capability failed")


@router.get("/catalog")
async def agent_catalog(user=Depends(require_agent_key)):
    """Every tool the agent may call: fixed primitives plus declared integrations."""
    consumer_id = get_settings().AGENT_CONSUMER_ID
    try:
        catalog = module_registry.integration_catalog(consumer_id=consumer_id)
    except IntegrationUnavailableError as exc:
        raise _fail(exc) from exc
    return {
        "consumer_id": consumer_id,
        "tools": [tool.model_dump(mode="json") for tool in build_tool_catalog(catalog)],
    }


@router.post("/invoke", response_model=AgentInvokeResponse)
async def agent_invoke(
    body: AgentInvokeRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_agent_key),
):
    """Call a declared integration with a validated request payload."""
    try:
        parameters = module_registry.validate_integration_request(
            body.integration_id,
            body.parameters,
            _context(db, user, body.consumer_id),
        )
    except (
        IntegrationNotFoundError,
        IntegrationRejectedError,
        IntegrationUnavailableError,
        ValidationError,
    ) as exc:
        raise _fail(exc) from exc
    if len(str(parameters).encode()) > MAX_INVOKE_PARAMETERS_BYTES:
        raise HTTPException(status_code=422, detail="The request was rejected")
    try:
        result = await module_registry.invoke_integration(
            body.integration_id,
            parameters,
            _context(db, user, body.consumer_id),
        )
    except (
        IntegrationRejectedError,
        IntegrationServiceError,
        IntegrationUnavailableError,
        ValidationError,
    ) as exc:
        raise _fail(exc) from exc
    return AgentInvokeResponse(result=result)


def _resource_integration(module_id: str, consumer_id: str) -> str | None:
    """Find the integration that can resolve content for a module."""
    try:
        catalog = module_registry.integration_catalog(consumer_id=consumer_id)
    except IntegrationUnavailableError:
        return None
    for item in catalog:
        if item["module_id"] == module_id and item.get("resource_schema"):
            return item["id"]
    return None


def _resource_text(resource: IntegrationResource, max_chars: int) -> dict[str, Any]:
    if resource.kind != "text":
        return {
            "ref": "",
            "kind": resource.kind,
            "title": resource.title,
            "text": "",
            "truncated": False,
            "pages_count": resource.pages_count,
        }
    text, truncated = _clip(resource.text or "", max_chars)
    return {
        "ref": "",
        "kind": resource.kind,
        "title": resource.title,
        "text": text,
        "truncated": truncated,
        "pages_count": resource.pages_count,
    }


@router.post("/resource")
async def agent_resource(
    body: AgentResourceRequest,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_agent_key),
):
    """Read the content behind a result: chapter text, page text or a description."""
    integration_id = _resource_integration(body.module_id, body.consumer_id)
    if not integration_id:
        raise HTTPException(status_code=404, detail="The requested capability is unavailable")
    try:
        resource = await module_registry.resolve_integration_resource(
            integration_id,
            {"item_id": body.item_id, "child_id": body.child_id, "page": body.page},
            _context(db, user, body.consumer_id),
        )
    except (
        IntegrationNotFoundError,
        IntegrationRejectedError,
        IntegrationServiceError,
        IntegrationUnavailableError,
        ValidationError,
    ) as exc:
        raise _fail(exc) from exc
    payload = _resource_text(resource, min(body.max_chars, MAX_RESOURCE_TEXT))
    payload["ref"] = body.ref
    return payload
