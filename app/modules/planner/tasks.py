"""Reminder delivery for the planner.

There is no Celery beat in this stack, so the sweep re-arms itself every minute,
mirroring miku.continue_tasks. Any planner write re-arms a dead loop through
ensure_sweep_armed, so a worker restart heals itself on the next API call.
"""

import asyncio
import logging

from app.core.database import AsyncSessionLocal
from app.core.scheduler import celery_app
from app.core.security import redis_client
from app.modules.planner.sweep import sweep_due_reminders

logger = logging.getLogger(__name__)

SELF_RESCHEDULE_SECONDS = 60
SWEEP_ARMED_KEY = "planner:sweep:armed"
SWEEP_ARMED_TTL_SECONDS = 150


async def ensure_sweep_armed() -> None:
    """Arm the reminder loop unless it is already running. Never raises."""
    try:
        armed = await redis_client.set(SWEEP_ARMED_KEY, "1", ex=SWEEP_ARMED_TTL_SECONDS, nx=True)
    except Exception:
        return
    if armed:
        try:
            sweep_reminders.apply_async(countdown=SELF_RESCHEDULE_SECONDS)
        except Exception:
            logger.warning("planner sweep could not be armed", exc_info=True)


async def _pass() -> int:
    async with AsyncSessionLocal() as session:
        pushed = await sweep_due_reminders(session)
    try:
        await redis_client.set(SWEEP_ARMED_KEY, "1", ex=SWEEP_ARMED_TTL_SECONDS)
    except Exception:
        pass
    return pushed


@celery_app.task(name="planner.sweep_reminders", ignore_result=True, max_retries=0)
def sweep_reminders() -> None:
    """Push due reminders, roll repeating events, then arm the next pass."""
    try:
        pushed = asyncio.run(_pass())
    except Exception:
        logger.exception("planner sweep pass failed")
        pushed = 0
    if pushed:
        logger.info("planner sweep pushed %d reminders", pushed)
    sweep_reminders.apply_async(countdown=SELF_RESCHEDULE_SECONDS)


__all__ = ["ensure_sweep_armed", "sweep_reminders"]
