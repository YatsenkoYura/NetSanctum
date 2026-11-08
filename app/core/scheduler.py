"""
Central Celery configuration.

Registers task modules declared by active module manifests.
"""

import asyncio
import logging

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger, worker_process_init

from app.core.config import get_settings
from app.core.modules import module_registry
from app.core.observability import configure_observability, process_role

logger = logging.getLogger(__name__)

settings = get_settings()
configure_observability(process_role("worker"))


celery_app = Celery(
    "netsanctum",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=module_registry.task_modules(),
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    worker_hijack_root_logger=False,
    # Emergency leak containment: recycle prefork children so RSS/RAM
    # accumulated in a worker process is actually returned to the OS.
    # Without these, yt-dlp/ffmpeg/SQLAlchemy growth is never reclaimed.
    worker_max_tasks_per_child=20,
    worker_max_memory_per_child=1048576,  # 1 GiB, in KiB
    task_acks_on_failure_or_timeout=True,
    result_expires=3600,
)


@after_setup_logger.connect(weak=False)
@after_setup_task_logger.connect(weak=False)
def configure_worker_logging(logger=None, **kwargs):
    if logger is not None:
        configure_observability(process_role("worker"), logger)


@worker_process_init.connect(weak=False)
def reset_worker_process_state(**kwargs):
    """Prefork children must not inherit the parent's DB pool or event loop."""
    from app.core.database import async_engine, sync_engine
    from app.core.worker_loop import reset_worker_loop

    reset_worker_loop()
    try:
        sync_engine.dispose(close=False)
    except Exception:
        logger.warning("could not dispose inherited sync engine", exc_info=True)

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(async_engine.dispose())
    except Exception:
        logger.warning("could not dispose inherited async engine", exc_info=True)
    finally:
        try:
            loop.close()
        except Exception:
            pass
        asyncio.set_event_loop(None)
