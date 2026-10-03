"""Background work for the assistant.

There is no Celery beat in this stack, so the continuation pass schedules itself: it
claims the goals that are due, retries them, and re-arms for later. That keeps the
initiative feature on the existing worker without adding another container.
"""

import asyncio
import logging

from celery.signals import worker_ready

from app.core.database import AsyncSessionLocal
from app.core.modules import module_registry
from app.core.scheduler import celery_app
from app.core.security import OwnerUser, redis_client
from app.modules.miku.cascades import claim_due_tasks, finish_task
from app.modules.miku.consolidate import consolidate_old_conversations
from app.modules.miku.notifications import push_notification
from app.modules.miku.schemas import MikuQuery
from app.modules.miku.service import MikuSessionContext, query

logger = logging.getLogger(__name__)

SELF_RESCHEDULE_SECONDS = 15 * 60
CONTINUE_ARMED_KEY = "miku:continue:armed"
CONTINUE_ARMED_TTL_SECONDS = 40 * 60
MAX_TASKS_PER_PASS = 5


async def _resume(session, task) -> None:
    """Retry one parked goal on its own; the user is not waiting on this."""
    await query(
        MikuQuery(message=task.goal),
        session,
        OwnerUser(id=task.user_id),
        module_registry,
        context=MikuSessionContext(),
        request_id=f"task-{task.id}",
    )
    await finish_task(session, task, done=True)
    await push_notification(task.user_id, f"Готово: {task.goal[:200]}")


async def continue_due_tasks() -> int:
    """Resume every goal whose moment has come; returns how many were attempted."""
    async with AsyncSessionLocal() as session:
        tasks = await claim_due_tasks(session, MAX_TASKS_PER_PASS)
        for task in tasks:
            try:
                await _resume(session, task)
            except Exception as exc:
                logger.warning("task %s could not be resumed: %s", task.id, type(exc).__name__)
                await finish_task(session, task, done=False, error=type(exc).__name__)
                await push_notification(task.user_id, f"Не смогла: {task.goal[:200]}. Попробую позже.")
        try:
            consolidated = await consolidate_old_conversations(session)
        except Exception:
            logger.exception("MIKU consolidation pass failed")
            consolidated = 0
        await session.commit()
        if consolidated:
            logger.info("miku consolidated %d conversations", consolidated)
        try:
            await redis_client.set(CONTINUE_ARMED_KEY, "1", ex=CONTINUE_ARMED_TTL_SECONDS)
        except Exception:
            pass
        return len(tasks)


async def ensure_continue_armed() -> None:
    """Arm the continuation loop unless it is already running. Never raises."""
    try:
        armed = await redis_client.set(CONTINUE_ARMED_KEY, "1", ex=CONTINUE_ARMED_TTL_SECONDS, nx=True)
    except Exception:
        return
    if armed:
        try:
            continue_tasks.apply_async(countdown=SELF_RESCHEDULE_SECONDS)
        except Exception:
            logger.warning("miku continuation could not be armed", exc_info=True)


@celery_app.task(name="miku.continue_tasks", ignore_result=True, max_retries=0)
def continue_tasks() -> None:
    """Claim due goals, then arm the next pass so the loop survives restarts."""
    asyncio.run(continue_due_tasks())
    continue_tasks.apply_async(countdown=SELF_RESCHEDULE_SECONDS)


@worker_ready.connect(weak=False)
def arm_continue_on_worker_ready(**kwargs) -> None:
    """First kick after a (re)start. SETNX-gated: never forks a second loop."""
    try:
        asyncio.run(ensure_continue_armed())
    except Exception:
        logger.warning("miku continuation could not be armed on worker ready", exc_info=True)
