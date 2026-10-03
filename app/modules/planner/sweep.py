import datetime
import logging

from sqlalchemy import select

from app.core.notifications import push_notification, push_notification_sync
from app.modules.planner import services
from app.modules.planner.models import PlannerEvent, PlannerTask

logger = logging.getLogger(__name__)

SWEEP_LIMIT = 20


def _label(kind: str, name: str | None) -> str:
    return name if kind != "none" and name else "inbox"


def _task_text(task: PlannerTask) -> str:
    moment = task.due_at or task.remind_at
    when = moment.astimezone(datetime.UTC).strftime("%d.%m %H:%M") if moment else "срок не указан"
    return f"Напоминание ({_label(task.space_kind, task.space_name)}): {task.title} — {when}"


def _event_text(event: PlannerEvent) -> str:
    when = event.starts_at.astimezone(datetime.UTC).strftime("%d.%m %H:%M")
    return f"Напоминание ({_label(event.space_kind, event.space_name)}): {event.title} — {when}"


async def sweep_due_reminders(session) -> int:
    """Push every due reminder once, then roll repeating events that already passed.

    Returns how many reminders were pushed. Never raises into the task loop:
    one bad row must not silence the rest.
    """
    now = datetime.datetime.now(datetime.UTC)
    pushed = 0
    tasks = list(
        (
            await session.execute(
                select(PlannerTask)
                .where(
                    PlannerTask.status.in_(services.OPEN_TASK_STATUSES),
                    PlannerTask.remind_at.is_not(None),
                    PlannerTask.remind_at <= now,
                    PlannerTask.notified_at.is_(None),
                )
                .order_by(PlannerTask.remind_at.asc())
                .limit(SWEEP_LIMIT)
            )
        ).scalars()
    )
    for task in tasks:
        try:
            await push_notification(task.user_id, _task_text(task))
            task.notified_at = now
            pushed += 1
        except Exception:
            logger.warning("planner reminder %s failed", task.id, exc_info=True)
    events = list(
        (
            await session.execute(
                select(PlannerEvent)
                .where(
                    PlannerEvent.status == "active",
                    PlannerEvent.remind_at.is_not(None),
                    PlannerEvent.remind_at <= now,
                    PlannerEvent.notified_at.is_(None),
                )
                .order_by(PlannerEvent.remind_at.asc())
                .limit(SWEEP_LIMIT)
            )
        ).scalars()
    )
    for event in events:
        try:
            await push_notification(event.user_id, _event_text(event))
            event.notified_at = now
            pushed += 1
        except Exception:
            logger.warning("planner reminder %s failed", event.id, exc_info=True)
    rolled = await _roll_past_events(session, now)
    await session.commit()
    return pushed + rolled


def sweep_due_reminders_sync(session) -> int:
    """The Celery-side twin of `sweep_due_reminders`.

    A worker cannot reuse the async engine: its pool hands out connections tied
    to the event loop that opened them, and a task that wraps its work in
    `asyncio.run` gets a fresh loop every minute. The failure surfaces as
    "got Future attached to a different loop" and silently stops the sweep, so
    worker code stays on the synchronous engine and client throughout.
    """
    now = datetime.datetime.now(datetime.UTC)
    pushed = 0
    tasks = list(
        session.execute(
            select(PlannerTask)
            .where(
                PlannerTask.status.in_(services.OPEN_TASK_STATUSES),
                PlannerTask.remind_at.is_not(None),
                PlannerTask.remind_at <= now,
                PlannerTask.notified_at.is_(None),
            )
            .order_by(PlannerTask.remind_at.asc())
            .limit(SWEEP_LIMIT)
        ).scalars()
    )
    for task in tasks:
        try:
            push_notification_sync(task.user_id, _task_text(task))
            task.notified_at = now
            pushed += 1
        except Exception:
            logger.warning("planner reminder %s failed", task.id, exc_info=True)
    events = list(
        session.execute(
            select(PlannerEvent)
            .where(
                PlannerEvent.status == "active",
                PlannerEvent.remind_at.is_not(None),
                PlannerEvent.remind_at <= now,
                PlannerEvent.notified_at.is_(None),
            )
            .order_by(PlannerEvent.remind_at.asc())
            .limit(SWEEP_LIMIT)
        ).scalars()
    )
    for event in events:
        try:
            push_notification_sync(event.user_id, _event_text(event))
            event.notified_at = now
            pushed += 1
        except Exception:
            logger.warning("planner reminder %s failed", event.id, exc_info=True)
    rolled = _roll_past_events_sync(session, now)
    session.commit()
    return pushed + rolled


def _roll_events(session, rows, now: datetime.datetime) -> int:
    """Close repeating events that already ended and spawn their next instance.

    Pure with respect to I/O: the caller has already loaded the rows, which is
    what lets the async and the sync sweep share it instead of duplicating the
    rollover rules.
    """
    rolled = 0
    for event in rows:
        end = event.ends_at or event.starts_at
        aware_end = end if end.tzinfo is not None else end.replace(tzinfo=datetime.UTC)
        if aware_end > now:
            continue
        following = services.next_occurrence(event.starts_at, event.recurrence)
        if following is None:
            continue
        shift = following - event.starts_at
        event.status = "done"
        session.add(
            PlannerEvent(
                user_id=event.user_id,
                title=event.title,
                notes=event.notes,
                location=event.location,
                starts_at=following,
                ends_at=(event.ends_at + shift) if event.ends_at else None,
                remind_at=(event.remind_at + shift) if event.remind_at else None,
                recurrence=event.recurrence,
                space_kind=event.space_kind,
                space_id=event.space_id,
                space_name=event.space_name,
                raw_text=event.raw_text,
            )
        )
        rolled += 1
    return rolled


def _repeating_events_statement(now: datetime.datetime):
    return select(PlannerEvent).where(
        PlannerEvent.status == "active",
        PlannerEvent.recurrence != "none",
        PlannerEvent.starts_at <= now,
    )


async def _roll_past_events(session, now: datetime.datetime) -> int:
    rows = list((await session.execute(_repeating_events_statement(now))).scalars())
    return _roll_events(session, rows, now)


def _roll_past_events_sync(session, now: datetime.datetime) -> int:
    rows = list(session.execute(_repeating_events_statement(now)).scalars())
    return _roll_events(session, rows, now)


__all__ = ["sweep_due_reminders", "sweep_due_reminders_sync"]
