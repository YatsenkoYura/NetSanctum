"""Shared helpers for reading what a provider integration already exposes."""

from typing import Any

from app.core.module_types import IntegrationUnavailableError

CONSUMER_REQUIRED = "Internal integration resources require a declared consumer"


def resource_integration_for(
    registry: Any,
    module_id: str,
    consumer_id: str,
) -> str | None:
    """Find the integration that can resolve content for a module, if any."""
    try:
        catalog = registry.integration_catalog(consumer_id=consumer_id)
    except IntegrationUnavailableError:
        return None
    for item in catalog:
        if item.get("module_id") == module_id and item.get("resource_schema"):
            return item["id"]
    return None


__all__ = ["CONSUMER_REQUIRED", "resource_integration_for"]
