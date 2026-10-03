"""Reminder delivery for the planner.

There is no Celery beat in this stack, so the sweep re-arms itself every minute,
mirroring miku.continue_tasks. Any planner write re-arms a dead loop through
ensure_sweep_armed, so a worker restart heals itself on the next API call.

Everything inside a task is synchronous on purpose. `asyncio.run` would give
every pass a brand-new event loop, while the async engine's pool hands out
connections bound to the loop that opened them — the second pass then dies with
"got Future attached to a different loop" and the sweep stops for good. Web
requests keep using the async session; only the worker path is synchronous.
"""

import logging

from celery.signals import worker_ready

from app.core.database import SyncSessionLocal
from app.core.notifications import sync_redis_client
from app.core.scheduler import celery_app
from app.core.security import redis_client
from app.modules.planner.sweep import sweep_due_reminders_sync

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


def ensure_sweep_armed_sync() -> None:
    """Worker-side twin of `ensure_sweep_armed`. Never raises."""
    try:
        armed = sync_redis_client().set(SWEEP_ARMED_KEY, "1", ex=SWEEP_ARMED_TTL_SECONDS, nx=True)
    except Exception:
        return
    if armed:
        try:
            sweep_reminders.apply_async(countdown=SELF_RESCHEDULE_SECONDS)
        except Exception:
            logger.warning("planner sweep could not be armed", exc_info=True)


def _pass() -> int:
    with SyncSessionLocal() as session:
        pushed = sweep_due_reminders_sync(session)
    try:
        sync_redis_client().set(SWEEP_ARMED_KEY, "1", ex=SWEEP_ARMED_TTL_SECONDS)
    except Exception:
        pass
    return pushed


@celery_app.task(name="planner.sweep_reminders", ignore_result=True, max_retries=0)
def sweep_reminders() -> None:
    """Push due reminders, roll repeating events, then arm the next pass."""
    try:
        pushed = _pass()
    except Exception:
        logger.exception("planner sweep pass failed")
        pushed = 0
    if pushed:
        logger.info("planner sweep pushed %d reminders", pushed)
    sweep_reminders.apply_async(countdown=SELF_RESCHEDULE_SECONDS)


@worker_ready.connect(weak=False)
def arm_sweep_on_worker_ready(**kwargs) -> None:
    """First kick after a (re)start. SETNX-gated: never forks a second loop."""
    try:
        ensure_sweep_armed_sync()
    except Exception:
        logger.warning("planner sweep could not be armed on worker ready", exc_info=True)


__all__ = [
    "arm_sweep_on_worker_ready",
    "ensure_sweep_armed",
    "ensure_sweep_armed_sync",
    "sweep_reminders",
]
