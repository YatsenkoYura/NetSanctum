import calendar
import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.planner.models import PlannerEvent, PlannerTask
from app.modules.planner.schemas import EventCreate, EventUpdate, TaskCreate, TaskUpdate

DEFAULT_TZ = ZoneInfo("Europe/Moscow")
OPEN_TASK_STATUSES = ("todo", "doing")
TASK_LIST_LIMIT = 200


def normalize_dt(value: datetime.datetime | None) -> datetime.datetime | None:
    """Store everything as aware UTC; a naive wall time means the owner's clock."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=DEFAULT_TZ)
    return value.astimezone(datetime.UTC)


def next_occurrence(moment: datetime.datetime, recurrence: str) -> datetime.datetime | None:
    """Deterministic repeat step in UTC. The model never computes this."""
    if recurrence == "daily":
        return moment + datetime.timedelta(days=1)
    if recurrence == "weekly":
        return moment + datetime.timedelta(weeks=1)
    if recurrence == "weekdays":
        candidate = moment + datetime.timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += datetime.timedelta(days=1)
        return candidate
    if recurrence == "monthly":
        year, month = moment.year, moment.month + 1
        if month > 12:
            year, month = year + 1, 1
        last_day = calendar.monthrange(year, month)[1]
        return moment.replace(year=year, month=month, day=min(moment.day, last_day))
    return None


def _aware_row(row):
    """SQLite returns naive datetimes; Postgres returns aware ones.

    The rest of the code compares and serializes in UTC, so coerce on the way
    out and never think about the backend again.
    """
    for field in (
        "due_at",
        "remind_at",
        "notified_at",
        "starts_at",
        "ends_at",
        "created_at",
        "updated_at",
        "completed_at",
    ):
        value = getattr(row, field, None)
        if isinstance(value, datetime.datetime) and value.tzinfo is None:
            setattr(row, field, value.replace(tzinfo=datetime.UTC))
    return row


async def create_task(session: AsyncSession, user_id: int, data: TaskCreate) -> PlannerTask:
    task = PlannerTask(
        user_id=user_id,
        title=data.title.strip(),
        notes=(data.notes or None),
        priority=data.priority,
        due_at=normalize_dt(data.due_at),
        remind_at=normalize_dt(data.remind_at),
        recurrence=data.recurrence,
        space_kind=data.space_kind,
        space_id=data.space_id,
        space_name=(data.space_name or None),
        raw_text=(data.raw_text or None),
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return _aware_row(task)


async def get_task(session: AsyncSession, user_id: int, task_id: int) -> PlannerTask | None:
    task = await session.scalar(
        select(PlannerTask).where(PlannerTask.id == task_id, PlannerTask.user_id == user_id)
    )
    return _aware_row(task) if task is not None else None


async def list_tasks(
    session: AsyncSession,
    user_id: int,
    *,
    statuses: tuple[str, ...] | None = None,
    overdue_only: bool = False,
    space_kind: str | None = None,
    space_id: int | None = None,
    due_before: datetime.datetime | None = None,
    due_after: datetime.datetime | None = None,
    limit: int = TASK_LIST_LIMIT,
) -> list[PlannerTask]:
    statement = select(PlannerTask).where(PlannerTask.user_id == user_id)
    if statuses:
        statement = statement.where(PlannerTask.status.in_(statuses))
    if overdue_only:
        now = datetime.datetime.now(datetime.UTC)
        statement = statement.where(
            PlannerTask.status.in_(OPEN_TASK_STATUSES),
            PlannerTask.due_at.is_not(None),
            PlannerTask.due_at < now,
        )
    if space_kind:
        statement = statement.where(PlannerTask.space_kind == space_kind)
    if space_id is not None:
        statement = statement.where(PlannerTask.space_id == space_id)
    if due_before is not None:
        statement = statement.where(PlannerTask.due_at.is_not(None), PlannerTask.due_at <= due_before)
    if due_after is not None:
        statement = statement.where(PlannerTask.due_at.is_not(None), PlannerTask.due_at >= due_after)
    statement = statement.order_by(
        PlannerTask.due_at.is_(None),
        PlannerTask.due_at.asc(),
        PlannerTask.priority.desc(),
        PlannerTask.id.asc(),
    ).limit(limit)
    return [_aware_row(task) for task in (await session.execute(statement)).scalars()]


async def update_task(session: AsyncSession, task: PlannerTask, data: TaskUpdate) -> PlannerTask:
    payload = data.model_dump(exclude_unset=True)
    for field in ("due_at", "remind_at"):
        if field in payload:
            payload[field] = normalize_dt(payload[field])
    for key, value in payload.items():
        if key == "title" and isinstance(value, str):
            value = value.strip()
        setattr(task, key, value)
    task.updated_at = datetime.datetime.now(datetime.UTC)
    if payload.get("status") == "done" and task.completed_at is None:
        task.completed_at = datetime.datetime.now(datetime.UTC)
    if payload.get("status") in ("todo", "doing"):
        task.completed_at = None
    await session.commit()
    await session.refresh(task)
    return _aware_row(task)


async def delete_task(session: AsyncSession, task: PlannerTask) -> None:
    await session.delete(task)
    await session.commit()


async def complete_task(session: AsyncSession, task: PlannerTask) -> PlannerTask | None:
    """Close a task; a repeating one spawns its next instance and returns it."""
    _aware_row(task)
    task.status = "done"
    now = datetime.datetime.now(datetime.UTC)
    task.completed_at = now
    task.updated_at = now
    spawned = None
    moment = task.due_at or task.remind_at or now
    following = next_occurrence(moment, task.recurrence)
    if following is not None:
        spawned = PlannerTask(
            user_id=task.user_id,
            title=task.title,
            notes=task.notes,
            priority=task.priority,
            due_at=following,
            remind_at=following if task.remind_at is not None else None,
            recurrence=task.recurrence,
            space_kind=task.space_kind,
            space_id=task.space_id,
            space_name=task.space_name,
            raw_text=task.raw_text,
        )
        session.add(spawned)
    await session.commit()
    await session.refresh(task)
    if spawned is not None:
        await session.refresh(spawned)
        _aware_row(spawned)
    return spawned


async def create_event(session: AsyncSession, user_id: int, data: EventCreate) -> PlannerEvent:
    event = PlannerEvent(
        user_id=user_id,
        title=data.title.strip(),
        notes=(data.notes or None),
        location=(data.location or None),
        starts_at=normalize_dt(data.starts_at),
        ends_at=normalize_dt(data.ends_at),
        remind_at=normalize_dt(data.remind_at),
        recurrence=data.recurrence,
        space_kind=data.space_kind,
        space_id=data.space_id,
        space_name=(data.space_name or None),
        raw_text=(data.raw_text or None),
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return _aware_row(event)


async def get_event(session: AsyncSession, user_id: int, event_id: int) -> PlannerEvent | None:
    event = await session.scalar(
        select(PlannerEvent).where(PlannerEvent.id == event_id, PlannerEvent.user_id == user_id)
    )
    return _aware_row(event) if event is not None else None


async def list_events(
    session: AsyncSession,
    user_id: int,
    *,
    statuses: tuple[str, ...] | None = None,
    starts_before: datetime.datetime | None = None,
    starts_after: datetime.datetime | None = None,
    space_kind: str | None = None,
    space_id: int | None = None,
    limit: int = TASK_LIST_LIMIT,
) -> list[PlannerEvent]:
    statement = select(PlannerEvent).where(PlannerEvent.user_id == user_id)
    if statuses:
        statement = statement.where(PlannerEvent.status.in_(statuses))
    if starts_before is not None:
        statement = statement.where(PlannerEvent.starts_at <= starts_before)
    if starts_after is not None:
        statement = statement.where(PlannerEvent.starts_at >= starts_after)
    if space_kind:
        statement = statement.where(PlannerEvent.space_kind == space_kind)
    if space_id is not None:
        statement = statement.where(PlannerEvent.space_id == space_id)
    statement = statement.order_by(PlannerEvent.starts_at.asc(), PlannerEvent.id.asc()).limit(limit)
    return [_aware_row(event) for event in (await session.execute(statement)).scalars()]


async def update_event(session: AsyncSession, event: PlannerEvent, data: EventUpdate) -> PlannerEvent:
    payload = data.model_dump(exclude_unset=True)
    for field in ("starts_at", "ends_at", "remind_at"):
        if field in payload:
            payload[field] = normalize_dt(payload[field])
    for key, value in payload.items():
        if key == "title" and isinstance(value, str):
            value = value.strip()
        setattr(event, key, value)
    event.updated_at = datetime.datetime.now(datetime.UTC)
    await session.commit()
    await session.refresh(event)
    return _aware_row(event)


async def delete_event(session: AsyncSession, event: PlannerEvent) -> None:
    await session.delete(event)
    await session.commit()


__all__ = [
    "DEFAULT_TZ",
    "OPEN_TASK_STATUSES",
    "complete_task",
    "create_event",
    "create_task",
    "delete_event",
    "delete_task",
    "get_event",
    "get_task",
    "list_events",
    "list_tasks",
    "next_occurrence",
    "normalize_dt",
    "update_event",
    "update_task",
]
