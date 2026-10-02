import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.core.templates import templates
from app.modules.planner import services
from app.modules.planner.schemas import (
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
    now = datetime.datetime.now(datetime.UTC)
    today_start, tomorrow_start = _day_bounds(now)
    overdue = await services.list_tasks(db, user.id, overdue_only=True, limit=DASHBOARD_LIMIT)
    due_today = [
        task
        for task in await services.list_tasks(
            db,
            user.id,
            statuses=services.OPEN_TASK_STATUSES,
            due_before=tomorrow_start,
            limit=DASHBOARD_LIMIT,
        )
        if task.due_at is not None and task.due_at >= today_start
    ]
    upcoming = await services.list_tasks(
        db, user.id, statuses=services.OPEN_TASK_STATUSES, limit=DASHBOARD_LIMIT
    )
    upcoming = [task for task in upcoming if task not in overdue and task not in due_today][:DASHBOARD_LIMIT]
    done = await services.list_tasks(db, user.id, statuses=("done",), limit=DASHBOARD_LIMIT)
    events = await services.list_events(
        db,
        user.id,
        statuses=("active",),
        starts_after=now - datetime.timedelta(hours=2),
        limit=DASHBOARD_LIMIT,
    )
    return templates.TemplateResponse(
        request,
        "planner_dashboard.html",
        {
            "user": user,
            "lang": await _get_lang(request),
            "now": now,
            "overdue": overdue,
            "due_today": due_today,
            "upcoming": upcoming,
            "done": done,
            "events": events,
        },
    )


@router.get("/api/planner/tasks", response_model=list[TaskResponse])
async def api_list_tasks(
    status: str | None = Query(None, description="Comma-separated todo,doing,done,cancelled"),
    overdue: bool = Query(False),
    space_kind: str | None = Query(None),
    space_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
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
    limit: int = Query(50, ge=1, le=200),
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    statuses = tuple(part for part in (status or "").split(",") if part)
    if upcoming:
        statuses = ("active",)
    starts_after = datetime.datetime.now(datetime.UTC) if upcoming else None
    return await services.list_events(
        db,
        user.id,
        statuses=statuses or None,
        starts_after=starts_after,
        space_kind=space_kind,
        space_id=space_id,
        limit=limit,
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
