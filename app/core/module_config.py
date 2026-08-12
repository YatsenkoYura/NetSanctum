"""Persisted desired-state configuration for optional modules."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


class ModuleConfigSettings(Protocol):
    ENABLED_MODULES: str
    ENABLED_MODULES_FILE: str


def parse_module_ids(raw: str) -> set[str] | None:
    value = raw.strip()
    if not value or value in {"*", "all"}:
        return None
    return {module_id.strip() for module_id in value.split(",") if module_id.strip()}


def load_enabled_module_ids(
    settings: ModuleConfigSettings | None = None,
) -> tuple[set[str] | None, str]:
    settings = settings or get_settings()
    from_environment = parse_module_ids(settings.ENABLED_MODULES)
    if settings.ENABLED_MODULES.strip():
        return from_environment, "environment"

    path = Path(settings.ENABLED_MODULES_FILE)
    if not path.is_file():
        return None, "default"
    payload = json.loads(path.read_text())
    module_ids = payload.get("enabled_modules")
    if not isinstance(module_ids, list) or not all(isinstance(item, str) for item in module_ids):
        raise ValueError(f"Invalid enabled modules file: {path}")
    return set(module_ids), "file"


def save_enabled_module_ids(module_ids: set[str], settings: ModuleConfigSettings | None = None) -> None:
    settings = settings or get_settings()
    if settings.ENABLED_MODULES.strip():
        raise RuntimeError("ENABLED_MODULES is managed by the environment")

    path = Path(settings.ENABLED_MODULES_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "enabled_modules": sorted(module_ids),
                "updated_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        )
        + "\n"
    )
    temporary.replace(path)


def reset_enabled_module_ids(settings: ModuleConfigSettings | None = None) -> None:
    settings = settings or get_settings()
    if settings.ENABLED_MODULES.strip():
        raise RuntimeError("ENABLED_MODULES is managed by the environment")
    Path(settings.ENABLED_MODULES_FILE).unlink(missing_ok=True)
