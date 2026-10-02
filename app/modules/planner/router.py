import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.templates import templates
from app.modules.planner import services
from app.modules.planner.schemas import (
    CalendarResponse,
    EventCreate,
    EventResponse,
    EventUpdate,
    TaskCreate,
    TaskResponse,
    TaskUpdate,
)
from app.modules.planner.tasks import ensure_sweep_armed

router = APIRouter()

DASHBOARD_LIMIT = 50
CALENDAR_LIMIT = 500


def _parse_range_bound(raw: str | None, *, is_end: bool) -> datetime.datetime | None:
    """Accept an ISO datetime or a plain YYYY-MM-DD day (Moscow wall time)."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if len(text) == 10:
        day = datetime.date.fromisoformat(text)
        if is_end:
            moment = datetime.datetime(day.year, day.month, day.day, 23, 59, 59, 999999)
        else:
            moment = datetime.datetime(day.year, day.month, day.day, 0, 0, 0)
        return services.normalize_dt(moment)
    parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return services.normalize_dt(parsed)


async def _get_lang(request: Request) -> str:
    return request.cookies.get("lang", "ru")


def _day_bounds(now: datetime.datetime):
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + datetime.timedelta(days=1)


@router.get("/planner", response_class=HTMLResponse, include_in_schema=False)
async def planner_dashboard(
    request: Request,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # The calendar shell is rendered empty on purpose: the visible range
    # (month/week/agenda) is loaded client-side via /api/planner/calendar,
    # so the server never has to guess which 42 days the owner looks at.
    now = datetime.datetime.now(datetime.UTC)
    return templates.TemplateResponse(
        request,
        "planner_dashboard.html",
        {
            "user": user,
            "lang": await _get_lang(request),
            "now": now,
        },
    )


@router.get("/api/planner/tasks", response_model=list[TaskResponse])
async def api_list_tasks(
    status: str | None = Query(None, description="Comma-separated todo,doing,done,cancelled"),
    overdue: bool = Query(False),
    space_kind: str | None = Query(None),
    space_id: int | None = Query(None),
    due_from: str | None = Query(None, description="ISO datetime or YYYY-MM-DD, inclusive"),
    due_to: str | None = Query(None, description="ISO datetime or YYYY-MM-DD, inclusive"),
    limit: int = Query(50, ge=1, le=500),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    statuses = tuple(part for part in (status or "").split(",") if part) or None
    return await services.list_tasks(
        db,
        user.id,
        statuses=statuses,
        overdue_only=overdue,
        space_kind=space_kind,
        space_id=space_id,
        due_after=_parse_range_bound(due_from, is_end=False),
        due_before=_parse_range_bound(due_to, is_end=True),
        limit=limit,
    )


@router.post("/api/planner/tasks", response_model=TaskResponse)
async def api_create_task(
    payload: TaskCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await services.create_task(db, user.id, payload)
    await ensure_sweep_armed()
    return task


@router.patch("/api/planner/tasks/{task_id}", response_model=TaskResponse)
async def api_update_task(
    task_id: int,
    payload: TaskUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await services.get_task(db, user.id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    updated = await services.update_task(db, task, payload)
    await ensure_sweep_armed()
    return updated


@router.delete("/api/planner/tasks/{task_id}")
async def api_delete_task(
    task_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await services.get_task(db, user.id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    await services.delete_task(db, task)
    return {"status": "deleted", "id": task_id}


@router.post("/api/planner/tasks/{task_id}/complete")
async def api_complete_task(
    task_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await services.get_task(db, user.id, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    spawned = await services.complete_task(db, task)
    return {"status": "done", "id": task_id, "next_id": spawned.id if spawned else None}


@router.get("/api/planner/events", response_model=list[EventResponse])
async def api_list_events(
    status: str | None = Query(None, description="Comma-separated active,done,cancelled"),
    upcoming: bool = Query(False),
    space_kind: str | None = Query(None),
    space_id: int | None = Query(None),
    from_date: str | None = Query(None, alias="from", description="ISO datetime or YYYY-MM-DD"),
    to_date: str | None = Query(None, alias="to", description="ISO datetime or YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=500),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    statuses = tuple(part for part in (status or "").split(",") if part)
    if upcoming:
        statuses = ("active",)
    starts_after = _parse_range_bound(from_date, is_end=False)
    starts_before = _parse_range_bound(to_date, is_end=True)
    if upcoming and starts_after is None:
        starts_after = datetime.datetime.now(datetime.UTC)
    return await services.list_events(
        db,
        user.id,
        statuses=statuses or None,
        starts_after=starts_after,
        starts_before=starts_before,
        space_kind=space_kind,
        space_id=space_id,
        limit=limit,
    )


@router.get("/api/planner/calendar", response_model=CalendarResponse)
async def api_calendar(
    from_date: str | None = Query(None, alias="from", description="ISO datetime or YYYY-MM-DD"),
    to_date: str | None = Query(None, alias="to", description="ISO datetime or YYYY-MM-DD"),
    space_kind: str | None = Query(None),
    space_id: int | None = Query(None),
    include_done_tasks: bool = Query(True),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """One payload for a visible calendar range: events plus dated tasks."""
    starts_after = _parse_range_bound(from_date, is_end=False)
    starts_before = _parse_range_bound(to_date, is_end=True)
    events = await services.list_events(
        db,
        user.id,
        statuses=("active",),
        starts_after=starts_after,
        starts_before=starts_before,
        space_kind=space_kind,
        space_id=space_id,
        limit=CALENDAR_LIMIT,
    )
    task_statuses: tuple[str, ...] = services.OPEN_TASK_STATUSES
    if include_done_tasks:
        task_statuses = (*services.OPEN_TASK_STATUSES, "done")
    tasks = await services.list_tasks(
        db,
        user.id,
        statuses=task_statuses,
        space_kind=space_kind,
        space_id=space_id,
        due_after=starts_after,
        due_before=starts_before,
        limit=CALENDAR_LIMIT,
    )
    return CalendarResponse(
        events=[EventResponse.model_validate(event) for event in events],
        tasks=[TaskResponse.model_validate(task) for task in tasks],
    )


@router.post("/api/planner/events", response_model=EventResponse)
async def api_create_event(
    payload: EventCreate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event = await services.create_event(db, user.id, payload)
    await ensure_sweep_armed()
    return event


@router.patch("/api/planner/events/{event_id}", response_model=EventResponse)
async def api_update_event(
    event_id: int,
    payload: EventUpdate,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event = await services.get_event(db, user.id, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    updated = await services.update_event(db, event, payload)
    await ensure_sweep_armed()
    return updated


@router.delete("/api/planner/events/{event_id}")
async def api_delete_event(
    event_id: int,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    event = await services.get_event(db, user.id, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await services.delete_event(db, event)
    return {"status": "deleted", "id": event_id}
