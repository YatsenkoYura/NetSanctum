"""
Central Celery configuration.

Registers task modules declared by active module manifests.
"""

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger

from app.core.config import get_settings
from app.core.modules import module_registry
from app.core.observability import configure_observability, process_role

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
)


@after_setup_logger.connect(weak=False)
@after_setup_task_logger.connect(weak=False)
def configure_worker_logging(logger=None, **kwargs):
    if logger is not None:
        configure_observability(process_role("worker"), logger)
