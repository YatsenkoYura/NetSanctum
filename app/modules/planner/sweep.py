import datetime
import logging

from sqlalchemy import select

from app.modules.miku.notifications import push_notification
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


async def _roll_past_events(session, now: datetime.datetime) -> int:
    """Close repeating events that already ended and spawn their next instance."""
    passed = list(
        (
            await session.execute(
                select(PlannerEvent).where(
                    PlannerEvent.status == "active",
                    PlannerEvent.recurrence != "none",
                    PlannerEvent.starts_at <= now,
                )
            )
        ).scalars()
    )
    rolled = 0
    for event in passed:
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


__all__ = ["sweep_due_reminders"]
