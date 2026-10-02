import datetime

from sqlalchemy import select

from app.contracts.planner_v1 import (
    PlannerCompleteTaskRequest,
    PlannerCompleteTaskResult,
    PlannerCreateEventRequest,
    PlannerCreateEventResult,
    PlannerCreateTaskRequest,
    PlannerCreateTaskResult,
    PlannerEventItem,
    PlannerListEventsRequest,
    PlannerListEventsResult,
    PlannerListTasksRequest,
    PlannerListTasksResult,
    PlannerResolveSpaceRequest,
    PlannerResolveSpaceResult,
    PlannerSnoozeTaskRequest,
    PlannerSnoozeTaskResult,
    PlannerSpaceRef,
    PlannerTaskItem,
    PlannerTodayRequest,
    PlannerTodayResult,
)
from app.contracts.undo_v1 import UndoRequest, UndoResult
from app.contracts.vault_spaces_v1 import VaultSpacesRequest, VaultSpacesResult
from app.core.module_types import IntegrationContext
from app.modules.planner import services
from app.modules.planner.models import PlannerEvent, PlannerTask
from app.modules.planner.schemas import EventCreate, SpaceKind, TaskCreate, TaskUpdate

RECURRENCE_VALUES = ("none", "daily", "weekdays", "weekly", "monthly")


class _BadDateError(ValueError):
    """Raised when the model sends a datetime no calendar understands."""


def _parse_dt(raw: str | None) -> datetime.datetime | None:
    if raw is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise _BadDateError(str(raw))
    return services.normalize_dt(parsed)


def _fmt_dt(value: datetime.datetime | None) -> str:
    if value is None:
        return "no date"
    moment = value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _space_label(kind: str, name: str | None) -> str:
    if kind == "none" or not name:
        return "inbox"
    return name


async def _load_spaces(context: IntegrationContext) -> VaultSpacesResult | None:
    try:
        result = await context.registry.invoke_integration(
            "vault.spaces.v1",
            VaultSpacesRequest().model_dump(mode="json"),
            IntegrationContext(
                session=context.session,
                user=context.user,
                registry=context.registry,
                consumer_id="planner",
            ),
        )
        return VaultSpacesResult.model_validate(result)
    except Exception:
        return None


def _resolve_space(
    spaces: VaultSpacesResult | None, name: str
) -> tuple[SpaceKind, int | None, str | None, list[str]]:
    """Fuzzy-match a spoken space name. Returns kind, id, name, candidates."""
    if spaces is None:
        return "none", None, None, []
    wanted = name.strip().casefold()
    exact = [space for space in spaces.spaces if space.name.casefold() == wanted]
    if len(exact) == 1:
        space = exact[0]
        return space.kind, space.id, space.name, []
    suffix = [space for space in spaces.spaces if space.path.casefold().endswith(wanted)]
    if len(suffix) == 1:
        space = suffix[0]
        return space.kind, space.id, space.name, []
    partial = [space for space in spaces.spaces if wanted in space.path.casefold()]
    if len(partial) == 1:
        space = partial[0]
        return space.kind, space.id, space.name, []
    return "none", None, None, [space.path for space in (exact or suffix or partial)[:5]]


def _task_item(task: PlannerTask) -> PlannerTaskItem:
    return PlannerTaskItem(
        id=task.id,
        title=task.title,
        status=task.status,
        priority=task.priority,
        due_at=task.due_at.isoformat() if task.due_at else None,
        space_name=task.space_name,
        recurrence=task.recurrence,
    )


def _event_item(event: PlannerEvent) -> PlannerEventItem:
    return PlannerEventItem(
        id=event.id,
        title=event.title,
        status=event.status,
        starts_at=event.starts_at.isoformat() if event.starts_at else "",
        location=event.location,
        space_name=event.space_name,
        recurrence=event.recurrence,
    )


async def create_task(
    request: PlannerCreateTaskRequest,
    context: IntegrationContext,
) -> PlannerCreateTaskResult:
    try:
        due_at = _parse_dt(request.due_at)
        remind_at = _parse_dt(request.remind_at)
    except _BadDateError as exc:
        return PlannerCreateTaskResult(
            status="invalid",
            message=f"Bad datetime {exc}: send ISO datetime like 2026-10-05T09:00:00+03:00.",
        )
    if request.recurrence not in RECURRENCE_VALUES:
        return PlannerCreateTaskResult(
            status="invalid",
            message=f"Bad recurrence {request.recurrence!r}: one of {', '.join(RECURRENCE_VALUES)}.",
        )
    space_kind, space_id, space_name = "none", None, None
    if request.space:
        spaces = await _load_spaces(context)
        if spaces is None:
            return PlannerCreateTaskResult(
                status="invalid",
                message="Vault spaces are unavailable right now; create the task without a space.",
            )
        space_kind, space_id, space_name, candidates = _resolve_space(spaces, request.space)
        if space_id is None:
            known = ", ".join(space.path for space in spaces.spaces[:10]) or "no spaces yet"
            hint = f" Did you mean: {', '.join(candidates)}?" if candidates else ""
            return PlannerCreateTaskResult(
                status="invalid",
                message=f"Unknown space {request.space!r}.{hint} Known spaces: {known}.",
            )
    if request.remind and due_at is not None:
        remind_at = due_at
    task = await services.create_task(
        context.session,
        context.user.id,
        TaskCreate(
            title=request.title,
            priority=request.priority,
            due_at=due_at,
            remind_at=remind_at,
            recurrence=request.recurrence,
            space_kind=space_kind,
            space_id=space_id,
            space_name=space_name,
            raw_text=request.title,
        ),
    )
    return PlannerCreateTaskResult(
        task_id=task.id,
        message=f"Task #{task.id}: {task.title} — {_fmt_dt(task.due_at)} "
        f"({_space_label(space_kind, space_name)}).",
    )


async def list_tasks(
    request: PlannerListTasksRequest,
    context: IntegrationContext,
) -> PlannerListTasksResult:
    user_id = context.user.id
    space_kind = space_id = None
    if request.space:
        spaces = await _load_spaces(context)
        if spaces is None:
            return PlannerListTasksResult(status="invalid", message="Vault spaces are unavailable right now.")
        space_kind, space_id, _, candidates = _resolve_space(spaces, request.space)
        if space_id is None:
            hint = f" Did you mean: {', '.join(candidates)}?" if candidates else ""
            return PlannerListTasksResult(status="invalid", message=f"Unknown space {request.space!r}.{hint}")
    if request.view == "overdue":
        tasks = await services.list_tasks(context.session, user_id, overdue_only=True, limit=request.limit)
    elif request.view == "upcoming":
        tasks = await services.list_tasks(
            context.session,
            user_id,
            statuses=services.OPEN_TASK_STATUSES,
            space_kind=space_kind,
            space_id=space_id,
            limit=request.limit,
        )
    elif request.view == "all":
        tasks = await services.list_tasks(
            context.session,
            user_id,
            space_kind=space_kind,
            space_id=space_id,
            limit=request.limit,
        )
    else:
        now = datetime.datetime.now(datetime.UTC)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
        tasks = [
            task
            for task in await services.list_tasks(
                context.session,
                user_id,
                statuses=services.OPEN_TASK_STATUSES,
                space_kind=space_kind,
                space_id=space_id,
                limit=request.limit,
            )
            if task.due_at is None or task.due_at <= end
        ]
    return PlannerListTasksResult(tasks=[_task_item(task) for task in tasks])


async def complete_task(
    request: PlannerCompleteTaskRequest,
    context: IntegrationContext,
) -> PlannerCompleteTaskResult:
    task = await services.get_task(context.session, context.user.id, request.task_id)
    if task is None:
        return PlannerCompleteTaskResult(
            status="invalid",
            message=f"No task #{request.task_id}. List tasks first to get a valid id.",
        )
    spawned = await services.complete_task(context.session, task)
    message = f"Task #{task.id} done."
    if spawned is not None:
        message += f" Next instance is #{spawned.id} at {_fmt_dt(spawned.due_at)}."
    return PlannerCompleteTaskResult(next_id=spawned.id if spawned else None, message=message)


async def snooze_task(
    request: PlannerSnoozeTaskRequest,
    context: IntegrationContext,
) -> PlannerSnoozeTaskResult:
    task = await services.get_task(context.session, context.user.id, request.task_id)
    if task is None:
        return PlannerSnoozeTaskResult(
            status="invalid",
            message=f"No task #{request.task_id}. List tasks first to get a valid id.",
        )
    try:
        remind_at = _parse_dt(request.remind_at)
    except _BadDateError:
        return PlannerSnoozeTaskResult(status="invalid", message="Bad remind_at: send ISO datetime.")
    if remind_at is None:
        return PlannerSnoozeTaskResult(status="invalid", message="Bad remind_at: send ISO datetime.")
    await services.update_task(context.session, task, TaskUpdate(remind_at=remind_at, status="todo"))
    return PlannerSnoozeTaskResult(message=f"Task #{task.id} will remind at {_fmt_dt(remind_at)}.")


async def create_event(
    request: PlannerCreateEventRequest,
    context: IntegrationContext,
) -> PlannerCreateEventResult:
    try:
        starts_at = _parse_dt(request.starts_at)
        ends_at = _parse_dt(request.ends_at)
        remind_at = _parse_dt(request.remind_at)
    except _BadDateError as exc:
        return PlannerCreateEventResult(
            status="invalid",
            message=f"Bad datetime {exc}: send ISO datetime like 2026-10-05T09:00:00+03:00.",
        )
    if starts_at is None:
        return PlannerCreateEventResult(
            status="invalid",
            message="Bad starts_at: send ISO datetime like 2026-10-05T09:00:00+03:00.",
        )
    if request.recurrence not in RECURRENCE_VALUES:
        return PlannerCreateEventResult(
            status="invalid",
            message=f"Bad recurrence {request.recurrence!r}: one of {', '.join(RECURRENCE_VALUES)}.",
        )
    space_kind, space_id, space_name = "none", None, None
    if request.space:
        spaces = await _load_spaces(context)
        if spaces is None:
            return PlannerCreateEventResult(
                status="invalid",
                message="Vault spaces are unavailable right now; create the event without a space.",
            )
        space_kind, space_id, space_name, candidates = _resolve_space(spaces, request.space)
        if space_id is None:
            hint = f" Did you mean: {', '.join(candidates)}?" if candidates else ""
            return PlannerCreateEventResult(
                status="invalid", message=f"Unknown space {request.space!r}.{hint}"
            )
    if request.remind:
        remind_at = starts_at
    event = await services.create_event(
        context.session,
        context.user.id,
        EventCreate(
            title=request.title,
            starts_at=starts_at,
            ends_at=ends_at,
            location=request.location,
            remind_at=remind_at,
            recurrence=request.recurrence,
            space_kind=space_kind,
            space_id=space_id,
            space_name=space_name,
            raw_text=request.title,
        ),
    )
    return PlannerCreateEventResult(
        event_id=event.id,
        message=f"Event #{event.id}: {event.title} — {_fmt_dt(event.starts_at)} "
        f"({_space_label(space_kind, space_name)}).",
    )


async def list_events(
    request: PlannerListEventsRequest,
    context: IntegrationContext,
) -> PlannerListEventsResult:
    user_id = context.user.id
    space_kind = space_id = None
    if request.space:
        spaces = await _load_spaces(context)
        if spaces is None:
            return PlannerListEventsResult(
                status="invalid", message="Vault spaces are unavailable right now."
            )
        space_kind, space_id, _, candidates = _resolve_space(spaces, request.space)
        if space_id is None:
            hint = f" Did you mean: {', '.join(candidates)}?" if candidates else ""
            return PlannerListEventsResult(
                status="invalid", message=f"Unknown space {request.space!r}.{hint}"
            )
    horizon = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=request.days)
    events = await services.list_events(
        context.session,
        user_id,
        statuses=("active",),
        starts_before=horizon,
        space_kind=space_kind,
        space_id=space_id,
        limit=50,
    )
    return PlannerListEventsResult(events=[_event_item(event) for event in events])


async def today(
    request: PlannerTodayRequest,
    context: IntegrationContext,
) -> PlannerTodayResult:
    lines, overdue_count, today_count = await agenda_lines(context.session, context.user.id)
    return PlannerTodayResult(lines=lines, overdue_count=overdue_count, today_count=today_count)


async def agenda_lines(session, user_id: int, *, max_lines: int = 12) -> tuple[list[str], int, int]:
    """Compact agenda snapshot, overdue first. Shared by the today tool and briefings."""
    now = datetime.datetime.now(datetime.UTC)
    today_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    overdue = await services.list_tasks(session, user_id, overdue_only=True, limit=50)
    open_tasks = await services.list_tasks(session, user_id, statuses=services.OPEN_TASK_STATUSES, limit=50)
    due_today = [task for task in open_tasks if task.due_at is not None and task.due_at <= today_end]
    events = await services.list_events(
        session, user_id, statuses=("active",), starts_before=today_end, limit=20
    )
    lines: list[str] = []
    for task in overdue[:5]:
        lines.append(
            f"OVERDUE: {task.title} (was {_fmt_dt(task.due_at)}, "
            f"{_space_label(task.space_kind, task.space_name)})"
        )
    for task in due_today[:5]:
        lines.append(
            f"TODAY: {task.title} ({_fmt_dt(task.due_at)}, {_space_label(task.space_kind, task.space_name)})"
        )
    for event in events[:4]:
        lines.append(
            f"EVENT: {event.title} ({_fmt_dt(event.starts_at)}, "
            f"{_space_label(event.space_kind, event.space_name)})"
        )
    return lines[:max_lines], len(overdue), len(due_today)


async def resolve_space(
    request: PlannerResolveSpaceRequest,
    context: IntegrationContext,
) -> PlannerResolveSpaceResult:
    spaces = await _load_spaces(context)
    if spaces is None:
        return PlannerResolveSpaceResult(status="invalid", message="Vault spaces are unavailable right now.")
    kind, space_id, name, candidates = _resolve_space(spaces, request.name)
    if space_id is None:
        hint = f" Did you mean: {', '.join(candidates)}?" if candidates else ""
        return PlannerResolveSpaceResult(
            status="invalid",
            candidates=candidates,
            message=f"Unknown space {request.name!r}.{hint}",
        )
    return PlannerResolveSpaceResult(
        space=PlannerSpaceRef(kind=kind, id=space_id, name=name),
        message=f"{name} ({kind}).",
    )


async def undo_planner_write(
    request: UndoRequest,
    context: IntegrationContext,
) -> UndoResult:
    """Reverse a planner create or complete, addressed by the original arguments.

    A create is found by its title and moment; a complete carries its task id.
    """
    user_id = context.user.id
    arguments = request.arguments
    if "task_id" in arguments:
        try:
            task_id = int(arguments["task_id"])
        except (TypeError, ValueError):
            return UndoResult(status="not_addressable", detail="The complete call had no task id")
        task = await services.get_task(context.session, user_id, task_id)
        if task is None or task.status != "done" or task.completed_at is None:
            return UndoResult(status="missing", detail="The task is already open or gone")
        reopened_at = task.completed_at
        await services.update_task(context.session, task, TaskUpdate(status="todo", remind_at=task.remind_at))
        twins = await context.session.execute(
            select(PlannerTask).where(
                PlannerTask.user_id == user_id,
                PlannerTask.title == task.title,
                PlannerTask.recurrence == task.recurrence,
                PlannerTask.status.in_(services.OPEN_TASK_STATUSES),
                PlannerTask.created_at > reopened_at,
            )
        )
        for twin in twins.scalars():
            await context.session.delete(twin)
        await context.session.commit()
        return UndoResult(status="undone", detail=f"Reopened task #{task.id}")
    title = str(arguments.get("title") or "").strip()[:200]
    if not title:
        return UndoResult(status="not_addressable", detail="The create call stored no title")
    if "starts_at" in arguments:
        statement = (
            select(PlannerEvent)
            .where(
                PlannerEvent.user_id == user_id,
                PlannerEvent.title == title,
                PlannerEvent.status == "active",
            )
            .order_by(PlannerEvent.id.desc())
            .limit(1)
        )
        event = await context.session.scalar(statement)
        if event is None:
            return UndoResult(status="missing", detail="The event is already gone")
        await context.session.delete(event)
        await context.session.commit()
        return UndoResult(status="undone", detail=f"Removed event #{event.id}")
    statement = (
        select(PlannerTask)
        .where(
            PlannerTask.user_id == user_id,
            PlannerTask.title == title,
            PlannerTask.status.in_(services.OPEN_TASK_STATUSES),
        )
        .order_by(PlannerTask.id.desc())
        .limit(1)
    )
    task = await context.session.scalar(statement)
    if task is None:
        return UndoResult(status="missing", detail="The task is already gone")
    await context.session.delete(task)
    await context.session.commit()
    return UndoResult(status="undone", detail=f"Removed task #{task.id}")


__all__ = [
    "agenda_lines",
    "complete_task",
    "create_event",
    "create_task",
    "list_events",
    "list_tasks",
    "resolve_space",
    "snooze_task",
    "today",
    "undo_planner_write",
]
