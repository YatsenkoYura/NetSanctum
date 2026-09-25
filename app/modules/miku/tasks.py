"""Background work for the assistant.

There is no Celery beat in this stack, so the continuation pass schedules itself: it
claims the goals that are due, retries them, and re-arms for later. That keeps the
initiative feature on the existing worker without adding another container.
"""

import asyncio
import logging

from app.core.database import AsyncSessionLocal
from app.core.modules import module_registry
from app.core.scheduler import celery_app
from app.core.security import OwnerUser
from app.modules.miku.cascades import claim_due_tasks, finish_task
from app.modules.miku.schemas import MikuQuery
from app.modules.miku.service import MikuSessionContext, query

logger = logging.getLogger(__name__)

SELF_RESCHEDULE_SECONDS = 15 * 60
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
        await session.commit()
        return len(tasks)


@celery_app.task(name="miku.continue_tasks", ignore_result=True, max_retries=0)
def continue_tasks() -> None:
    """Claim due goals, then arm the next pass so the loop survives restarts."""
    asyncio.run(continue_due_tasks())
    continue_tasks.apply_async(countdown=SELF_RESCHEDULE_SECONDS)
