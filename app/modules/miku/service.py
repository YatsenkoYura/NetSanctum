"""The assistant's session layer.

All reasoning happens in the isolated agent runtime as a cascade. This module keeps
what only the application can do: the owner's session, the transcript, the reference
cards the browser can open, the audit trail and the resource endpoint used to display
content.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent.catalog import tool_name
from app.core.agent.resources import resource_integration_for
from app.core.control_center import tracked_tasks
from app.core.module_types import (
    IntegrationContext,
    IntegrationRejectedError,
    IntegrationResource,
)
from app.modules.miku.agent_turn import run_agent_turn
from app.modules.miku.cascades import open_task, record_cascade, reply_outcome, steps_from_result
from app.modules.miku.models import MikuTurnAudit
from app.modules.miku.providers import load_provider_bundle
from app.modules.miku.schemas import (
    MikuCapabilities,
    MikuConversationTurn,
    MikuJobStatus,
    MikuProvider,
    MikuQuery,
    MikuReference,
    MikuReply,
    MikuReplySegment,
)

CONSUMER_ID = "miku"
HISTORY_TURNS = 6
MAX_REFERENCES = 20
logger = logging.getLogger(__name__)

TurnEventHook = Callable[[str, dict], Awaitable[None]]

PRIMITIVE_TOOLS = ("read", "fetch", "act", "ask", "final")


class MikuQueryError(ValueError):
    pass


@dataclass
class MikuSessionContext:
    references: list[MikuReference] | None = None
    history: list[Any] | None = None


class _Registry(Protocol):
    def integration_catalog(self, consumer_id: str | None = None) -> list[dict[str, Any]]: ...

    async def resolve_integration_resource(
        self,
        integration_id: str,
        payload: dict[str, Any],
        context: IntegrationContext,
    ) -> IntegrationResource: ...

    def storage_owner(self, namespace: str) -> str | None: ...


def audit_turn(db: AsyncSession, user, request_id: str, transport: str, reply: MikuReply) -> None:
    """Metadata only: the transcript is never written to the audit table."""
    db.add(
        MikuTurnAudit(
            user_id=user.id,
            request_id=request_id,
            transport=transport,
            command=reply.command,
            result_count=len(reply.references),
            warning_count=len(reply.warnings),
        )
    )


def capabilities(registry: _Registry) -> MikuCapabilities:
    """What the assistant can do: its primitives plus the declared integrations."""
    tools: list[str] = []
    providers: list[MikuProvider] = []
    for item in registry.integration_catalog(consumer_id=CONSUMER_ID):
        effects = item.get("effects") or {}
        tools.append(item["id"])
        providers.append(
            MikuProvider(
                module_id=item["module_id"],
                integration_id=item["id"],
                contract=item.get("contract") or "search.query.v1",
            )
        )
        if effects.get("effect") == "read":
            continue
    return MikuCapabilities(
        commands=[*PRIMITIVE_TOOLS, *sorted(tools)],
        providers=providers,
    )


def _remember(context: MikuSessionContext | None, request: MikuQuery, reply: MikuReply) -> MikuReply:
    """Keep the transcript bounded: six turns is what the model gets to see."""
    if context is None:
        return reply
    history = list(context.history or [])
    history.append(MikuConversationTurn(user=request.message[:500], assistant=reply.text[:500]))
    context.history = history[-HISTORY_TURNS:]
    context.references = list(reply.references)[:MAX_REFERENCES]
    return reply


def unavailable_reply() -> MikuReply:
    """Honest failure: no rule planner, no canned command list, no pretending."""
    return MikuReply(
        command="respond",
        text="Ассистент сейчас недоступен. Попробуй ещё раз через минуту.",
        segments=[
            MikuReplySegment(
                kind="status",
                text="Ассистент сейчас недоступен. Попробуй ещё раз через минуту.",
            )
        ],
    )


async def query(
    request: MikuQuery,
    db: AsyncSession,
    user,
    registry: _Registry,
    context: MikuSessionContext | None = None,
    on_event: TurnEventHook | None = None,
    request_id: str = "",
    *,
    remember: bool = True,
) -> MikuReply:
    """Run one cascade for the owner, streaming progress while it happens."""

    async def _emit(phase: str, data: dict) -> None:
        if on_event is None:
            return
        try:
            await on_event(phase, data)
        except Exception:
            logger.exception("MIKU turn event hook failed")

    providers = await load_provider_bundle(db, user.id) if db is not None and user is not None else None
    effects = _catalog_effects(registry)

    async def _store(turn, reply) -> None:
        await record_cascade(
            db,
            user,
            request_id=request_id or uuid4().hex,
            goal=request.message,
            steps=steps_from_result(turn),
            skeleton=list(turn.skeleton),
            answer=reply.text,
            exhausted=turn.exhausted,
            effects=effects,
        )
        if turn.exhausted:
            # An unfinished goal is not lost: a background pass comes back to it.
            await open_task(db, user, request.message, cursor=reply_outcome(reply))
        await db.flush()

    reply = await run_agent_turn(
        request,
        context,
        provider=providers.llm if providers else None,
        on_event=_emit,
        on_turn=_store if db is not None else None,
    )
    if reply is None:
        return unavailable_reply()
    return _remember(context, request, reply) if remember else reply


def _catalog_effects(registry: _Registry) -> dict[str, dict[str, Any]]:
    """Reversibility is declared by the module, so the log knows what can be undone."""
    try:
        catalog = registry.integration_catalog(consumer_id=CONSUMER_ID)
    except Exception:
        return {}
    return {tool_name(item["id"]): item.get("effects") or {} for item in catalog}


async def resolve_resource(
    module_id: str,
    item_id: str,
    child_id: str | None,
    page: int | None,
    db: AsyncSession,
    user,
    registry: _Registry,
) -> IntegrationResource:
    """Read what a provider exposes for one item, without leaking storage paths."""
    integration_id = resource_integration_for(registry, module_id, CONSUMER_ID)
    if not integration_id:
        raise MikuQueryError("Unknown or unavailable resource provider")
    resource = await registry.resolve_integration_resource(
        integration_id,
        {"item_id": item_id, "child_id": child_id, "page": page},
        IntegrationContext(session=db, user=user, registry=registry, consumer_id=CONSUMER_ID),
    )
    if resource.storage_path:
        parts = resource.storage_path.split("/")
        if (
            resource.storage_path.startswith("/")
            or "\\" in resource.storage_path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise IntegrationRejectedError("Provider returned an invalid storage resource")
        if registry.storage_owner(parts[0]) != module_id:
            raise IntegrationRejectedError("Provider returned a foreign storage resource")
    return resource


async def job_status(task_id: str) -> MikuJobStatus | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        raise MikuQueryError("Invalid task ID")
    task = next((item for item in await tracked_tasks() if item.get("task_id") == task_id), None)
    if not task:
        return None
    return MikuJobStatus(
        task_id=task_id,
        module_id=str(task.get("module") or "unknown")[:63],
        status=str(task.get("status") or "running")[:120] or "running",
        progress=str(task.get("progress") or "")[:32],
        title=str(task.get("title") or "")[:160],
    )


__all__ = [
    "MikuQueryError",
    "MikuSessionContext",
    "TurnEventHook",
    "audit_turn",
    "capabilities",
    "job_status",
    "query",
    "resolve_resource",
    "unavailable_reply",
]
