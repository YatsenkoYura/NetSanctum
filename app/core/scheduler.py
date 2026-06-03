"""
Central Celery configuration.

Dynamically discovers tasks from all `app.modules.*.tasks` sub-modules
so that every module's tasks are registered without manual imports.
"""

import importlib
import pkgutil

from celery import Celery

from app.core.config import get_settings

settings = get_settings()


def _discover_task_modules() -> list[str]:
    """Scan app/modules/*/tasks.py and return dotted module paths."""
    task_modules: list[str] = []
    try:
        import app.modules as modules_pkg

        for importer, module_name, is_pkg in pkgutil.iter_modules(
            modules_pkg.__path__, prefix="app.modules."
        ):
            if is_pkg:
                tasks_path = f"{module_name}.tasks"
                try:
                    importlib.import_module(tasks_path)
                    task_modules.append(tasks_path)
                except Exception as e:
                    # Module doesn't have tasks.py or dependencies are missing — that's fine
                    pass
    except ImportError:
        pass

    return task_modules


celery_app = Celery(
    "netsanctum",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
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
)

# Auto-discover tasks from all modules
celery_app.autodiscover_tasks(_discover_task_modules)
