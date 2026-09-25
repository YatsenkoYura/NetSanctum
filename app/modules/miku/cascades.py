"""Persistence for cascades: what ran, how to undo it, and what is still unfinished.

Every turn is written once, after the cascade finishes. Mutating steps keep enough
information to be undone through the integration that declares an undo, and goals the
agent could not finish stay in miku_task so a background pass can pick them up.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.module_types import (
    IntegrationContext,
    IntegrationServiceError,
    IntegrationUnavailableError,
)
from app.modules.miku.models import MikuCascadeLog, MikuTask
from app.modules.miku.schemas import MikuReply

logger = logging.getLogger(__name__)

MAX_GOAL_LENGTH = 2_000
MAX_STEPS_STORED = 20
MAX_ANSWER_LENGTH = 2_000
MAX_CASCADE_LIST = 50
MAX_ARGUMENT_KEYS = 8
MAX_ARGUMENT_TEXT = 500
MAX_ARGUMENT_BYTES = 500
TASK_MAX_ATTEMPTS = 5
TASK_RETRY_MINUTES = 30
CONSUMER_ID = "miku"
MUTATING_EFFECTS = {"create", "update", "delete", "execute"}


def _aware(value: datetime | None) -> datetime | None:
    """Some backends hand back naive timestamps; comparisons must not explode."""
    if value is None or value.tzinfo is None:
        return value.replace(tzinfo=UTC) if value is not None else None
    return value


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def build_undo_plan(
    steps: list[dict[str, Any]],
    effects: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Remember the mutating steps and how to reverse them.

    Reversibility is declared by the provider, never guessed: a step that names an undo
    integration gets a plan, a step that does not is recorded as irreversible.
    """
    reversible: list[dict[str, Any]] = []
    irreversible: list[str] = []
    for step in steps:
        if step.get("status") not in {"success", "empty"}:
            continue
        tool = str(step.get("tool") or "")
        if tool in {"final", "ask"} or tool.startswith(("read", "fetch", "act")):
            continue
        effect = effects.get(tool) or {}
        if effect.get("effect") not in MUTATING_EFFECTS:
            continue
        undo_integration = effect.get("undo_integration")
        if undo_integration:
            reversible.append(
                {
                    "tool": tool,
                    "undo_integration": undo_integration,
                    "arguments": step.get("arguments") or {},
                }
            )
        else:
            irreversible.append(tool)
    return {"reversible": reversible, "irreversible": sorted(set(irreversible))}


async def record_cascade(
    db: AsyncSession,
    user,
    *,
    request_id: str,
    goal: str,
    steps: list[dict[str, Any]],
    skeleton: list[str],
    answer: str,
    exhausted: bool,
    effects: dict[str, dict[str, Any]],
) -> MikuCascadeLog:
    """Write one finished cascade; failures here must never break the turn."""
    entry = MikuCascadeLog(
        user_id=getattr(user, "id", 0) or 0,
        request_id=request_id[:64],
        goal=_clip(goal, MAX_GOAL_LENGTH),
        status="exhausted" if exhausted else "done",
        steps_json={"steps": (steps or [])[:MAX_STEPS_STORED]},
        skeleton_json=list(skeleton or [])[:20],
        answer=_clip(answer, MAX_ANSWER_LENGTH) or None,
        undo_json=build_undo_plan(steps or [], effects),
        created_at=datetime.now(UTC),
    )
    db.add(entry)
    return entry


async def undo_cascade_step(
    db: AsyncSession,
    user,
    cascade_id: int,
    index: int,
    registry,
) -> dict[str, Any]:
    """Run the undo integration a provider declared for one recorded step."""
    entry = (
        await db.scalars(
            select(MikuCascadeLog).where(
                MikuCascadeLog.id == cascade_id,
                MikuCascadeLog.user_id == getattr(user, "id", 0),
            )
        )
    ).first()
    if entry is None:
        raise LookupError("cascade not found")
    plan = (entry.undo_json or {}).get("reversible") or []
    if index < 0 or index >= len(plan):
        raise IndexError("step cannot be undone")
    step = plan[index]
    try:
        # Every undo integration takes the original arguments plus the flag to reverse.
        result = await registry.invoke_integration(
            step["undo_integration"],
            {"undo": True, "arguments": step.get("arguments") or {}},
            IntegrationContext(session=db, user=user, registry=registry, consumer_id=CONSUMER_ID),
        )
    except (IntegrationServiceError, IntegrationUnavailableError) as exc:
        raise RuntimeError("undo failed") from exc
    await db.flush()
    return {"cascade_id": cascade_id, "step": index, "result": result}


async def list_cascades(db: AsyncSession, user, limit: int = 20) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(
            select(MikuCascadeLog)
            .where(MikuCascadeLog.user_id == getattr(user, "id", 0))
            .order_by(MikuCascadeLog.created_at.desc())
            .limit(min(limit, MAX_CASCADE_LIST))
        )
    ).all()
    return [
        {
            "id": row.id,
            "request_id": row.request_id,
            "goal": row.goal,
            "status": row.status,
            "answer": row.answer,
            "steps": (row.steps_json or {}).get("steps", []),
            "undo": row.undo_json or {"reversible": [], "irreversible": []},
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


async def open_task(
    db: AsyncSession,
    user,
    goal: str,
    *,
    cursor: dict[str, Any] | None = None,
    delay_minutes: int = 0,
) -> MikuTask:
    """Park a goal the agent could not finish so a background pass can resume it."""
    task = MikuTask(
        user_id=getattr(user, "id", 0) or 0,
        goal=_clip(goal, MAX_GOAL_LENGTH),
        state="open",
        cursor_json=cursor or {},
        attempts=0,
        due_at=datetime.now(UTC) + timedelta(minutes=delay_minutes),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(task)
    await db.flush()
    return task


async def claim_due_tasks(db: AsyncSession, limit: int = 5) -> list[MikuTask]:
    """Take the tasks whose moment has come, so a worker cannot pick them twice."""
    now = datetime.now(UTC)
    candidates = (
        await db.scalars(
            select(MikuTask).where(MikuTask.state == "open").order_by(MikuTask.due_at).limit(limit * 4)
        )
    ).all()
    tasks = [task for task in candidates if (_aware(task.due_at) or now) <= now][:limit]
    for task in tasks:
        task.state = "running"
        task.attempts += 1
        task.updated_at = now
    await db.flush()
    return list(tasks)


async def finish_task(
    db: AsyncSession,
    task: MikuTask,
    *,
    done: bool,
    error: str | None = None,
) -> None:
    now = datetime.now(UTC)
    if done:
        task.state = "done"
        task.last_error = None
    elif task.attempts >= TASK_MAX_ATTEMPTS:
        task.state = "failed"
        task.last_error = _clip(error, 255) or "gave up"
    else:
        task.state = "open"
        task.last_error = _clip(error, 255)
        task.due_at = now + timedelta(minutes=TASK_RETRY_MINUTES)
    task.updated_at = now
    await db.flush()


def reply_outcome(reply: MikuReply) -> dict[str, Any]:
    """Small shape the log stores about the visible outcome of a turn."""
    return {
        "command": reply.command,
        "references": len(reply.references),
        "client_action": reply.client_action,
        "question": reply.question,
    }


def steps_from_result(result: Any) -> list[dict[str, Any]]:
    """Reduce engine step records to what is worth keeping on disk."""
    return [
        {
            "tool": step.tool,
            "status": step.status,
            "summary": _clip(step.summary, 300),
            "arguments": _safe_arguments(step.arguments),
        }
        for step in getattr(result, "steps", [])
    ]


def _safe_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Arguments may carry long text or nested objects; the log keeps them bounded."""
    if not arguments:
        return {}
    trimmed: dict[str, Any] = {}
    for key, value in list(arguments.items())[:MAX_ARGUMENT_KEYS]:
        if isinstance(value, str):
            trimmed[key] = value[:MAX_ARGUMENT_TEXT]
        elif isinstance(value, (int, float, bool)) or value is None:
            trimmed[key] = value
        else:
            try:
                encoded = json.dumps(value, default=str)
            except (TypeError, ValueError):
                trimmed[key] = str(value)[:MAX_ARGUMENT_TEXT]
                continue
            if len(encoded) <= MAX_ARGUMENT_BYTES:
                trimmed[key] = value
            else:
                trimmed[key] = {"truncated_bytes": len(encoded)}
    return trimmed


__all__ = [
    "MikuCascadeLog",
    "MikuTask",
    "build_undo_plan",
    "claim_due_tasks",
    "finish_task",
    "list_cascades",
    "open_task",
    "record_cascade",
    "reply_outcome",
    "steps_from_result",
    "undo_cascade_step",
]
