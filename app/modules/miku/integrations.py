"""Memory integrations exposed to the agent as ordinary tools."""

from datetime import UTC, datetime

from sqlalchemy import delete, select

from app.contracts.miku_memory_v1 import (
    MikuMemoryEntry,
    MikuMemorySearchRequest,
    MikuMemorySearchResult,
    MikuMemoryWriteRequest,
    MikuMemoryWriteResult,
)
from app.contracts.undo_v1 import UndoRequest, UndoResult
from app.core.module_types import (
    IntegrationContext,
    IntegrationRejectedError,
    IntegrationUnavailableError,
)
from app.core.text_match import query_terms, text_matches_terms
from app.modules.miku.models import MikuEpisodeMemory, MikuProfileMemory

SCOPES = {"profile", "episodic"}


def _aware(value: datetime | None) -> datetime | None:
    """Storage backends may hand back naive timestamps; comparisons must not explode."""
    if value is None or value.tzinfo is None:
        return value.replace(tzinfo=UTC) if value is not None else None
    return value


def _owner_id(context: IntegrationContext) -> int:
    user_id = getattr(context.user, "id", None)
    if not isinstance(user_id, int):
        raise IntegrationUnavailableError("Memory requires an authenticated owner")
    return user_id


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IntegrationRejectedError("Memory expiry must be an ISO timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def write_memory(
    request: MikuMemoryWriteRequest,
    context: IntegrationContext,
) -> MikuMemoryWriteResult:
    """Store or remove one memory item for the owner."""
    user_id = _owner_id(context)
    if request.scope == "profile":
        if request.op == "delete":
            result = await context.session.execute(
                delete(MikuProfileMemory).where(
                    MikuProfileMemory.user_id == user_id,
                    MikuProfileMemory.memory_key == request.key,
                )
            )
            return MikuMemoryWriteResult(
                status="deleted" if result.rowcount else "missing",
                scope="profile",
                key=request.key,
            )
        expires_at = _parse_expiry(request.expires_at)
        existing = await context.session.scalar(
            select(MikuProfileMemory).where(
                MikuProfileMemory.user_id == user_id,
                MikuProfileMemory.memory_key == request.key,
            )
        )
        if existing is None:
            context.session.add(
                MikuProfileMemory(
                    user_id=user_id,
                    memory_key=request.key,
                    value_json=request.value,
                    source=request.source,
                    confidence=request.confidence,
                    expires_at=expires_at,
                )
            )
        else:
            existing.value_json = request.value
            existing.source = request.source
            existing.confidence = request.confidence
            existing.expires_at = expires_at
            existing.updated_at = datetime.now(UTC)
        return MikuMemoryWriteResult(status="written", scope="profile", key=request.key)

    occurred_at = _parse_expiry(request.expires_at) or datetime.now(UTC)
    if request.op == "delete":
        result = await context.session.execute(
            delete(MikuEpisodeMemory).where(
                MikuEpisodeMemory.user_id == user_id,
                MikuEpisodeMemory.summary == request.summary,
            )
        )
        return MikuMemoryWriteResult(
            status="deleted" if result.rowcount else "missing",
            scope="episodic",
        )
    context.session.add(
        MikuEpisodeMemory(
            user_id=user_id,
            summary=request.summary or "",
            subject=request.subject,
            tags_json=request.tags,
            source=request.source,
            occurred_at=occurred_at,
        )
    )
    return MikuMemoryWriteResult(status="written", scope="episodic")


def _matches(value: str, terms: list[str]) -> bool:
    """Recall a memory from the words the user actually said.

    Matching is word based and tolerant of case endings, so a fact stored as
    "люблю пиццу" is still found by "пицца" and "rezero" still finds "Re:Zero".
    """
    return text_matches_terms(value, terms)


async def search_memory(
    request: MikuMemorySearchRequest,
    context: IntegrationContext,
) -> MikuMemorySearchResult:
    """Recall stored facts and episode summaries, newest and most confident first."""
    user_id = _owner_id(context)
    scopes = [scope for scope in request.scopes if scope in SCOPES] or ["profile", "episodic"]
    terms = query_terms(request.query)
    items: list[MikuMemoryEntry] = []
    now = datetime.now(UTC)

    if "profile" in scopes:
        statement = (
            select(MikuProfileMemory)
            .where(MikuProfileMemory.user_id == user_id)
            .order_by(MikuProfileMemory.updated_at.desc())
            .limit(request.limit)
        )
        for record in await context.session.scalars(statement):
            expires_at = _aware(record.expires_at)
            if expires_at and expires_at <= now:
                continue
            if not _matches(f"{record.memory_key} {record.value_json}", terms):
                continue
            items.append(
                MikuMemoryEntry(
                    scope="profile",
                    key=record.memory_key,
                    value=record.value_json or {},
                    summary=(record.value_json or {}).get("summary"),
                    source=record.source,
                    confidence=record.confidence,
                    occurred_at=(_aware(record.updated_at) or record.updated_at).isoformat(),
                )
            )

    if "episodic" in scopes and len(items) < request.limit:
        statement = (
            select(MikuEpisodeMemory)
            .where(MikuEpisodeMemory.user_id == user_id)
            .order_by(MikuEpisodeMemory.occurred_at.desc())
            .limit(request.limit)
        )
        for record in await context.session.scalars(statement):
            if not _matches(f"{record.summary} {record.subject or ''}", terms):
                continue
            items.append(
                MikuMemoryEntry(
                    scope="episodic",
                    summary=record.summary,
                    subject=record.subject,
                    tags=list(record.tags_json or []),
                    source=record.source,
                    occurred_at=(_aware(record.occurred_at) or record.occurred_at).isoformat(),
                )
            )

    return MikuMemorySearchResult(items=items[: request.limit])


async def undo_memory_write(
    request: UndoRequest,
    context: IntegrationContext,
) -> UndoResult:
    """Forget the item a write added, addressed by the arguments of that write."""
    user_id = _owner_id(context)
    scope = str(request.arguments.get("scope") or "profile")
    key = request.arguments.get("key")
    summary = request.arguments.get("summary")
    if scope == "episodic" and summary:
        result = await context.session.execute(
            delete(MikuEpisodeMemory).where(
                MikuEpisodeMemory.user_id == user_id,
                MikuEpisodeMemory.summary == str(summary)[:500],
            )
        )
        await context.session.flush()
        return UndoResult(
            status="undone" if result.rowcount else "missing",
            detail="Forgot the episode",
        )
    if scope == "profile" and key:
        result = await context.session.execute(
            delete(MikuProfileMemory).where(
                MikuProfileMemory.user_id == user_id,
                MikuProfileMemory.memory_key == str(key)[:64],
            )
        )
        await context.session.flush()
        return UndoResult(
            status="undone" if result.rowcount else "missing",
            detail="Forgot the fact",
        )
    return UndoResult(status="not_addressable", detail="The memory write had no key or summary")
