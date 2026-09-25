"""Bridge from a MIKU turn to the isolated agent runtime.

MIKU keeps the session, the transcript and the reply shape; the cascade runs elsewhere
and reports what it did. If the runtime is not configured or not answering, the caller
falls back to the in-process assistant rather than dropping the turn.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.core.agent.engine import AgentHistoryTurn, AgentProviderProfile, AgentTurnResult
from app.core.agent.references import AgentReference
from app.core.agent_client import AgentClient, AgentRuntimeUnavailableError, AgentTurnProgress
from app.modules.miku.schemas import (
    MikuCommand,
    MikuQuery,
    MikuReference,
    MikuReply,
    MikuRuntimeCapabilities,
)

if TYPE_CHECKING:
    from app.modules.miku.service import MikuSessionContext

logger = logging.getLogger(__name__)

REPLY_TEXT_LIMIT = 500
HISTORY_TEXT_LIMIT = 500
STATUS_LIMIT = 200


def agent_client() -> AgentClient:
    from app.core.config import get_settings

    settings = get_settings()
    return AgentClient(
        enabled=settings.AGENT_RUNTIME_ENABLED,
        url=settings.AGENT_RUNTIME_URL,
        token=settings.AGENT_RUNTIME_TOKEN,
    )


def to_agent_references(references: list[MikuReference]) -> list[AgentReference]:
    """Only what the model may reason about: identity, kind and availability."""
    projected: list[AgentReference] = []
    for index, reference in enumerate(references[:20], 1):
        try:
            agent_reference = AgentReference(
                ref=reference.ref if reference.ref.startswith("result:") else f"result:{index}",
                module_id=reference.module_id,
                item_id=reference.item_id,
                kind=reference.kind,
                title=reference.title,
                subtitle=reference.subtitle,
                summary=reference.summary,
                entity_type=reference.entity_type,
                playable=reference.playable,
                readable=reference.readable,
                open_url=reference.open_url or reference.resource_url,
            )
        except ValueError:
            continue
        # A readable or playable result is always actionable, even without a stored URL.
        projected.append(
            agent_reference.model_copy(
                update={"open_url": agent_reference.open_url or agent_reference.resource_endpoint}
            )
        )
    return projected


def to_miku_references(result: AgentTurnResult) -> list[MikuReference]:
    wanted = list(result.refs)
    references = []
    for reference in result.references:
        if wanted and reference.ref not in wanted:
            continue
        references.append(
            MikuReference(
                ref=reference.ref,
                module_id=reference.module_id,
                item_id=reference.item_id,
                kind=reference.kind,
                title=reference.title,
                subtitle=reference.subtitle,
                summary=reference.summary,
                playable=reference.playable,
                readable=reference.readable,
                entity_type=reference.entity_type,
                open_url=reference.open_url,
                resource_url=reference.resource_endpoint,
            )
        )
    return references


def to_miku_reply(result: AgentTurnResult, *, command: MikuCommand = "respond") -> MikuReply:
    """Ground the answer: only references the agent actually produced are attached."""
    if result.question:
        return MikuReply(
            command=command,
            text=result.question[:REPLY_TEXT_LIMIT],
            question=result.question,
            question_options=list(result.question_options),
            references=to_miku_references(result),
            exhausted=result.exhausted,
        )
    answer = result.answer.strip() or "Готово."
    return MikuReply(
        command="play"
        if result.client_action == "play"
        else "open"
        if result.client_action == "open"
        else command,
        text=answer[:REPLY_TEXT_LIMIT],
        references=to_miku_references(result),
        client_action=result.client_action,
        exhausted=result.exhausted,
    )


def history_for_agent(context: "MikuSessionContext | None") -> list[AgentHistoryTurn]:
    if not context or not context.history:
        return []
    return [
        AgentHistoryTurn(
            user=turn.user[:HISTORY_TEXT_LIMIT],
            assistant=turn.assistant[:HISTORY_TEXT_LIMIT],
        )
        for turn in context.history[-6:]
    ]


async def runtime_capabilities(
    client: AgentClient,
    providers: object,
) -> "MikuRuntimeCapabilities":
    """What the isolated runtime can serve right now, merged with the user's settings."""
    if not client.enabled:
        return MikuRuntimeCapabilities(enabled=False)
    health: dict[str, object] = {}
    try:
        import httpx

        async with httpx.AsyncClient(timeout=3, transport=client.transport) as probe:
            response = await probe.get(f"{client.url}/health")
        if response.status_code == 200:
            health = response.json()
    except Exception:
        health = {}
    available = health.get("providers") if isinstance(health.get("providers"), dict) else {}
    llm, stt, tts = (
        getattr(providers, "llm", None),
        getattr(providers, "stt", None),
        getattr(providers, "tts", None),
    )
    return MikuRuntimeCapabilities(
        enabled=bool(available),
        llm=bool(available.get("llm")) or bool(getattr(llm, "server_callable", False)),
        stt=bool(available.get("stt")) or bool(getattr(stt, "server_callable", False)),
        tts=bool(available.get("tts")) or bool(getattr(tts, "server_callable", False)),
        modes={
            "llm": getattr(llm, "mode", "api"),
            "stt": getattr(stt, "mode", "api"),
            "tts": getattr(tts, "mode", "api"),
        },
    )


def _as_text(value: object) -> str:
    """Provider fields are strings; a stray type is dropped, never stringified.

    Dropping lets the sidecar fall back to the model it hosts, which beats sending a
    repr of a broken value to a provider.
    """
    return value.strip() if isinstance(value, str) else ""


def to_agent_profile(provider: object) -> AgentProviderProfile | None:
    """Hand the resolved per-user model settings to the sidecar for this call only."""
    if provider is None or not getattr(provider, "server_callable", False):
        return None
    try:
        return AgentProviderProfile(
            url=_as_text(getattr(provider, "url", "")),
            model=_as_text(getattr(provider, "model", "")),
            api_key=_as_text(getattr(provider, "api_key", "")),
            mode=_as_text(getattr(provider, "mode", "api")) or "api",
        )
    except (ValueError, TypeError):
        logger.warning("ignoring an unusable provider profile", exc_info=True)
        return None


async def run_agent_turn(
    request: MikuQuery,
    context: "MikuSessionContext | None",
    *,
    client: AgentClient | None = None,
    provider: object = None,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
    on_turn: Callable[[AgentTurnResult, MikuReply], Awaitable[None]] | None = None,
) -> MikuReply | None:
    """Execute one cascade; return None when the runtime cannot serve this turn."""
    agent = client or agent_client()
    if not agent.enabled:
        return None
    provider_profile = to_agent_profile(provider)
    statuses: list[str] = []
    warnings: list[str] = []

    async def _emit(phase: str, data: dict) -> None:
        if on_event is None:
            return
        try:
            await on_event(phase, data)
        except Exception:
            logger.exception("MIKU agent progress hook failed")

    turn: AgentTurnResult | None = None
    reply: MikuReply | None = None
    try:
        async for item in agent.turn(
            message=request.message,
            session_id=getattr(context, "session_id", "") or "",
            history=history_for_agent(context),
            references=to_agent_references(list((context.references if context else None) or [])),
            llm=provider_profile,
        ):
            if isinstance(item, AgentTurnProgress):
                if item.type == "error":
                    # The runtime reports transport failures here; log them once, bounded.
                    logger.warning(
                        "agent turn error: %s",
                        str(item.payload.get("reason") or "")[:200],
                    )
                if item.type == "step":
                    tool = str(item.payload.get("tool") or "")
                    summary = str(item.payload.get("summary") or "")[:STATUS_LIMIT]
                    if summary and summary not in statuses:
                        statuses.append(summary)
                        await _emit("acknowledgement", {"text": summary})
                    await _emit(
                        "tool_result",
                        {
                            "integration_id": tool or "agent",
                            "status": str(item.payload.get("status") or "empty"),
                            "result_count": int(item.payload.get("result_count") or 0),
                        },
                    )
                continue
            turn = item
            reply = to_miku_reply(item)
            if warnings or statuses:
                reply = reply.model_copy(update={"warnings": warnings})
            break
        if turn is not None and reply is not None and on_turn is not None:
            # Recording is best effort: a log failure must not cost the user an answer.
            try:
                await on_turn(turn, reply)
            except Exception:
                logger.exception("MIKU cascade logging failed")
        if reply is not None:
            return reply
    except AgentRuntimeUnavailableError as exc:
        logger.warning("agent runtime unavailable, falling back: %s", exc)
        return None
    return None


__all__ = [
    "agent_client",
    "history_for_agent",
    "run_agent_turn",
    "runtime_capabilities",
    "to_agent_profile",
    "to_agent_references",
    "to_miku_references",
    "to_miku_reply",
]
