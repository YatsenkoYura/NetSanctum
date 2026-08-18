"""Module manifests, discovery, activation, and runtime diagnostics."""

import importlib
import inspect
import logging
import pkgutil
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.module_config import load_enabled_module_ids
from app.core.module_types import (
    MODULE_API_VERSION,
    IntegrationContext,
    IntegrationSpec,
    IntegrationUnavailableError,
    ModuleSpec,
)

logger = logging.getLogger(__name__)

REQUIRED_MODULE_IDS = frozenset({"auth", "settings", "sharing"})


class ModuleStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    INCOMPATIBLE = "incompatible"


@dataclass(slots=True)
class ModuleRecord:
    """Discovered module plus its process-local runtime state."""

    package: str
    spec: ModuleSpec | None
    status: ModuleStatus
    error: str | None = None
    component_errors: dict[str, str] | None = None
    started: bool = False

    @property
    def id(self) -> str:
        return self.spec.id if self.spec else self.package.rsplit(".", 1)[-1]


class ModuleRegistry:
    """Single source of truth for installed and enabled modules."""

    def __init__(
        self,
        enabled_modules: set[str] | None = None,
        installed_modules: set[str] | None = None,
    ) -> None:
        self._enabled_modules = enabled_modules
        self._installed_modules = installed_modules
        self._records: dict[str, ModuleRecord] = {}
        self.unknown_enabled_ids: set[str] = set()
        self.unknown_installed_ids: set[str] = set()
        self.unavailable_enabled_ids: set[str] = set()

    @classmethod
    def discover(
        cls,
        enabled_modules: set[str] | None = None,
        installed_modules: set[str] | None = None,
        *,
        strict_enabled: bool = False,
        strict_installed: bool = False,
    ) -> "ModuleRegistry":
        registry = cls(enabled_modules, installed_modules)
        import app.modules as modules_package

        discovered_packages = sorted(
            module_name
            for _importer, module_name, is_package in pkgutil.iter_modules(
                modules_package.__path__, prefix="app.modules."
            )
            if is_package
        )
        for package in discovered_packages:
            registry._discover_package(package)
        registry._discover_entry_points()

        registry._validate_capabilities()
        if enabled_modules is not None:
            registry.unknown_enabled_ids = enabled_modules - set(registry._records)
            if registry.unknown_enabled_ids:
                logger.warning(
                    "Configured modules are not installed: %s",
                    ", ".join(sorted(registry.unknown_enabled_ids)),
                )
            registry.unavailable_enabled_ids = {
                module_id
                for module_id in enabled_modules
                if (record := registry._records.get(module_id)) and record.status == ModuleStatus.UNAVAILABLE
            }
        if installed_modules is not None:
            registry.unknown_installed_ids = installed_modules - set(registry._records)
        if strict_installed and registry.unknown_installed_ids:
            raise RuntimeError(
                "Modules recorded as installed are missing from the image: "
                + ", ".join(sorted(registry.unknown_installed_ids))
            )
        if strict_enabled and (registry.unknown_enabled_ids or registry.unavailable_enabled_ids):
            unavailable = registry.unknown_enabled_ids | registry.unavailable_enabled_ids
            raise RuntimeError(
                "Enabled modules are not installed in this image: " + ", ".join(sorted(unavailable))
            )
        registry._validate_required_manifests()
        return registry

    def _validate_capabilities(self) -> None:
        migration_owners: dict[str, ModuleRecord] = {}
        for record in self.declared_records():
            if not record.spec or not record.spec.migrations:
                continue
            for table in record.spec.migrations.owned_tables:
                owner = migration_owners.get(table)
                if owner:
                    raise RuntimeError(
                        f"Duplicate migration table {table!r} declared by {owner.id!r} and {record.id!r}"
                    )
                migration_owners[table] = record

        dashboards: dict[str, ModuleRecord] = {}
        storage_namespaces: dict[str, ModuleRecord] = {}
        package_prefixes: dict[str, ModuleRecord] = {}
        entity_types: dict[str, ModuleRecord] = {}
        integrations: dict[str, ModuleRecord] = {}
        ui_actions: dict[str, ModuleRecord] = {}
        for record in self.installed_records():
            spec = record.spec
            if not spec:
                continue
            declarations = (
                (spec.dashboard_url, dashboards, "dashboard URL") if spec.dashboard_url else None,
                *(
                    (namespace, storage_namespaces, "storage namespace")
                    for namespace in spec.storage_namespaces
                ),
                *((prefix, package_prefixes, "package prefix") for prefix in spec.package_prefixes),
                *((entity_type, entity_types, "entity type") for entity_type in spec.entity_types),
                *((item.id, integrations, "integration") for item in spec.integrations),
                *((item.id, ui_actions, "UI action") for item in spec.ui_actions),
            )
            for declaration in declarations:
                if declaration is None:
                    continue
                value, seen, label = declaration
                owner = seen.get(value)
                if owner:
                    error = ValueError(f"Duplicate {label} {value!r} also declared by {owner.id!r}")
                    self._fail(record, "manifest", error)
                    continue
                seen[value] = record

    def _validate_required_manifests(self) -> None:
        for module_id in REQUIRED_MODULE_IDS:
            record = self._records.get(module_id)
            if not record or not record.spec or record.status != ModuleStatus.ACTIVE:
                detail = record.error if record else "not installed"
                raise RuntimeError(f"Required module {module_id!r} is unavailable: {detail}")

    def _discover_package(self, package: str) -> None:
        package_id = package.rsplit(".", 1)[-1]
        try:
            manifest = importlib.import_module(f"{package}.module")
            self._register_spec(package, manifest.MODULE, expected_id=package_id)
        except Exception as exc:
            self._records[package_id] = ModuleRecord(
                package=package,
                spec=None,
                status=ModuleStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            logger.error("Failed to discover module %s: %s", package, exc)

    def _discover_entry_points(self) -> None:
        for entry_point in entry_points(group="netsanctum.modules"):
            if self._installed_modules is None or entry_point.name not in self._installed_modules:
                continue
            try:
                package = (
                    entry_point.module.removesuffix(".module")
                    if entry_point.module.endswith(".module")
                    else entry_point.module
                )
                self._register_spec(package, entry_point.load(), expected_id=entry_point.name)
            except Exception as exc:
                logger.error("Failed to load external module entry point %s: %s", entry_point.name, exc)

    def _register_spec(
        self,
        package: str,
        spec: ModuleSpec,
        *,
        expected_id: str | None = None,
    ) -> None:
        if not isinstance(spec, ModuleSpec):
            raise TypeError("module entry point must expose a ModuleSpec")
        if expected_id and spec.id != expected_id:
            raise ValueError(f"Manifest id {spec.id!r} does not match package {expected_id!r}")
        if spec.id in self._records:
            raise ValueError(f"Duplicate module id: {spec.id}")

        if spec.api_version != MODULE_API_VERSION:
            record = ModuleRecord(
                package=package,
                spec=spec,
                status=ModuleStatus.INCOMPATIBLE,
                error=f"Module API {spec.api_version} is not supported (expected {MODULE_API_VERSION})",
            )
        elif not spec.required and not spec.bundled and self._installed_modules is None:
            record = ModuleRecord(package=package, spec=spec, status=ModuleStatus.UNAVAILABLE)
        elif (
            not spec.required
            and self._installed_modules is not None
            and spec.id not in self._installed_modules
        ):
            record = ModuleRecord(package=package, spec=spec, status=ModuleStatus.UNAVAILABLE)
        else:
            enabled = spec.required or self._enabled_modules is None or spec.id in self._enabled_modules
            record = ModuleRecord(
                package=package,
                spec=spec,
                status=ModuleStatus.ACTIVE if enabled else ModuleStatus.DISABLED,
            )
        self._records[spec.id] = record

    @property
    def records(self) -> tuple[ModuleRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda record: (self._order(record), record.id)))

    @staticmethod
    def _order(record: ModuleRecord) -> int:
        return record.spec.order if record.spec else 10_000

    def active_records(self) -> tuple[ModuleRecord, ...]:
        return tuple(record for record in self.records if record.status == ModuleStatus.ACTIVE)

    def installed_records(self) -> tuple[ModuleRecord, ...]:
        return tuple(
            record
            for record in self.records
            if record.spec and record.status not in {ModuleStatus.UNAVAILABLE, ModuleStatus.INCOMPATIBLE}
        )

    def declared_records(self) -> tuple[ModuleRecord, ...]:
        return tuple(record for record in self.records if record.spec is not None)

    def is_active(self, module_id: str) -> bool:
        record = self._records.get(module_id)
        return bool(record and record.status == ModuleStatus.ACTIVE)

    def is_installed(self, module_id: str) -> bool:
        record = self._records.get(module_id)
        return bool(
            record
            and record.spec
            and record.status not in {ModuleStatus.UNAVAILABLE, ModuleStatus.INCOMPATIBLE}
        )

    def navigation(self) -> list[dict[str, Any]]:
        return [
            {
                "name": record.spec.id,
                "title_en": record.spec.title_en,
                "title_ru": record.spec.title_ru,
                "dashboard_url": record.spec.dashboard_url,
                "order": record.spec.order,
            }
            for record in self.active_records()
            if record.spec and record.spec.dashboard_url
        ]

    def load_routers(self) -> list[tuple[str, Any]]:
        routers = []
        for record in self.active_records():
            if not record.spec or not record.spec.router:
                continue
            try:
                routers.append((record.id, self._load_object(record.spec.router)))
            except Exception as exc:
                self._fail(record, "router", exc)
                if record.spec.required:
                    raise RuntimeError(f"Required module {record.id!r} router failed") from exc
        return routers

    def import_models(self, *, include_disabled: bool = False, strict: bool = False) -> None:
        records = self.declared_records() if include_disabled else self.active_records()
        for record in records:
            if not record.spec or not record.spec.models:
                continue
            try:
                importlib.import_module(record.spec.models)
            except Exception as exc:
                if strict or record.spec.required:
                    raise RuntimeError(f"Failed to import models for module {record.id}: {exc}") from exc
                self._fail(record, "models", exc)

    def task_modules(self) -> list[str]:
        return [record.spec.tasks for record in self.active_records() if record.spec and record.spec.tasks]

    def translation_modules(self) -> list[tuple[str, str]]:
        return [
            (record.id, record.spec.i18n)
            for record in self.active_records()
            if record.spec and record.spec.i18n
        ]

    def template_dirs(self) -> list[Path]:
        directories = []
        for record in self.active_records():
            if not record.spec or not record.spec.templates:
                continue
            package_module = importlib.import_module(record.package)
            package_file = getattr(package_module, "__file__", None)
            if not package_file:
                exc = RuntimeError("module package has no filesystem path")
                self._fail(record, "templates", exc)
                if record.spec.required:
                    raise RuntimeError(f"Required module {record.id!r} templates failed") from exc
                continue
            directory = Path(package_file).resolve().parent / record.spec.templates
            if not directory.is_dir():
                exc = FileNotFoundError(directory)
                self._fail(record, "templates", exc)
                if record.spec.required:
                    raise RuntimeError(f"Required module {record.id!r} templates failed") from exc
                continue
            directories.append(directory)
        return directories

    def progress_key_patterns(self) -> tuple[str, ...]:
        return tuple(
            pattern
            for record in self.declared_records()
            if record.spec
            for pattern in record.spec.progress_key_patterns
        )

    def storage_owner(self, namespace: str) -> str | None:
        for record in self.declared_records():
            if record.spec and namespace in record.spec.storage_namespaces:
                return record.id
        return None

    def file_cleanup_hook(self, namespace: str) -> Any | None:
        owner = self.storage_owner(namespace)
        record = self._records.get(owner) if owner else None
        if not record or not record.spec or not record.spec.file_cleanup:
            return None
        if record.status in {ModuleStatus.FAILED, ModuleStatus.INCOMPATIBLE}:
            return None
        try:
            return self._load_object(record.spec.file_cleanup)
        except Exception as exc:
            self._component_error(record, "file_cleanup", exc)
            return None

    def module_cleanup_hook(self, namespace: str) -> Any | None:
        owner = self.storage_owner(namespace)
        record = self._records.get(owner) if owner else None
        if not record or not record.spec:
            return None
        if record.status in {ModuleStatus.FAILED, ModuleStatus.INCOMPATIBLE}:
            return None
        if not record.spec.module_cleanup:
            return None
        try:
            return self._load_object(record.spec.module_cleanup)
        except Exception as exc:
            self._component_error(record, "module_cleanup", exc)
            return None

    def package_resolver(self, package_id: str) -> Any | None:
        providers = sorted(
            (
                (prefix, record)
                for record in self.active_records()
                if record.spec and record.spec.package_resolver
                for prefix in record.spec.package_prefixes
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        for prefix, record in providers:
            if package_id.startswith(prefix):
                spec = record.spec
                if not spec or not spec.package_resolver:
                    return None
                try:
                    return self._load_object(spec.package_resolver)
                except Exception as exc:
                    self._component_error(record, "package_resolver", exc)
                    return None
        return None

    def share_provider(self, module_id: str) -> Any | None:
        """Load the active module's optional, read-only sharing provider."""
        record = self._records.get(module_id)
        if (
            not record
            or record.status != ModuleStatus.ACTIVE
            or not record.spec
            or not record.spec.share_provider
        ):
            return None
        try:
            return self._load_object(record.spec.share_provider)
        except Exception as exc:
            self._component_error(record, "share_provider", exc)
            return None

    async def resolve_entity(self, entity_type: str, entity_id: str, session: Any) -> dict | None:
        for record in self.active_records():
            spec = record.spec
            if not spec or entity_type not in spec.entity_types or not spec.entity_resolver:
                continue
            try:
                resolver = self._load_object(spec.entity_resolver)
                return await resolver(session, entity_type, entity_id)
            except Exception as exc:
                logger.warning(
                    "Module %s could not resolve entity %s/%s: %s",
                    record.id,
                    entity_type,
                    entity_id,
                    exc,
                )
                return None
        return None

    def integration_provider(self, integration_id: str) -> tuple[ModuleRecord, IntegrationSpec] | None:
        for record in self.active_records():
            if not record.spec:
                continue
            for integration in record.spec.integrations:
                if integration.id == integration_id:
                    return record, integration
        return None

    def has_integration(self, integration_id: str) -> bool:
        return self.integration_provider(integration_id) is not None

    def integration_catalog(self) -> list[dict[str, Any]]:
        """Describe active integrations for API clients and diagnostics."""
        catalog = []
        for record in self.active_records():
            if not record.spec:
                continue
            for integration in record.spec.integrations:
                try:
                    request_model = self._load_object(integration.request_model)
                    result_model = self._load_object(integration.result_model)
                    catalog.append(
                        {
                            "id": integration.id,
                            "module_id": record.id,
                            "request_schema": request_model.model_json_schema(),
                            "result_schema": result_model.model_json_schema(),
                            "used_by": sorted(
                                consumer.id
                                for consumer in self.active_records()
                                if consumer.spec and integration.id in consumer.spec.uses_integrations
                            ),
                        }
                    )
                except Exception as exc:
                    self._component_error(record, f"integration:{integration.id}", exc)
        return sorted(catalog, key=lambda item: item["id"])

    async def invoke_integration(
        self,
        integration_id: str,
        payload: dict[str, Any],
        context: IntegrationContext,
    ) -> dict[str, Any]:
        """Validate and invoke an active module's versioned integration handler."""
        provider = self.integration_provider(integration_id)
        if not provider:
            raise IntegrationUnavailableError(f"No active provider for integration {integration_id!r}")

        record, integration = provider
        try:
            request_model = self._load_object(integration.request_model)
            result_model = self._load_object(integration.result_model)
            handler = self._load_object(integration.handler)
        except Exception as exc:
            self._component_error(record, f"integration:{integration_id}", exc)
            raise IntegrationUnavailableError(f"Integration {integration_id!r} could not be loaded") from exc

        request = request_model.model_validate(payload)
        result = handler(request, context)
        if inspect.isawaitable(result):
            result = await result
        try:
            validated_result = result_model.model_validate(result)
        except ValidationError as exc:
            raise RuntimeError(f"Integration {integration_id!r} returned an invalid result") from exc
        return validated_result.model_dump(mode="json")

    def ui_actions(self, slot: str, context: dict[str, str], lang: str = "en") -> list[dict[str, Any]]:
        """Return safe, structured actions contributed by active modules."""
        entity_type = context.get("entity_type")
        entity_owner = next(
            (
                record
                for record in self.active_records()
                if record.spec and entity_type in record.spec.entity_types
            ),
            None,
        )
        actions = []
        for record in self.active_records():
            if not record.spec:
                continue
            for action in record.spec.ui_actions:
                if action.slot != slot or not self.has_integration(action.integration):
                    continue
                if action.entity_types:
                    if entity_type not in action.entity_types:
                        continue
                    if (
                        not entity_owner
                        or not entity_owner.spec
                        or action.integration not in entity_owner.spec.uses_integrations
                    ):
                        continue
                actions.append(
                    {
                        "id": action.id,
                        "module_id": record.id,
                        "label": action.label_ru if lang == "ru" else action.label_en,
                        "integration": action.integration,
                        "method": "POST",
                        "href": f"/api/integrations/{action.integration}",
                        "payload": context,
                        "order": action.order,
                    }
                )
        return sorted(actions, key=lambda action: (action["order"], action["id"]))

    async def run_startup_hooks(self) -> None:
        for record in self.active_records():
            if not record.spec or not record.spec.startup:
                continue
            record.started = True
            try:
                result = self._load_object(record.spec.startup)()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self._fail(record, "startup", exc)
                if record.spec.required:
                    raise RuntimeError(f"Required module {record.id!r} startup failed") from exc

    async def run_shutdown_hooks(self) -> None:
        for record in reversed(self.records):
            if not record.started or not record.spec or not record.spec.shutdown:
                continue
            try:
                result = self._load_object(record.spec.shutdown)()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.error("Module %s shutdown failed: %s", record.id, exc)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "module_api_version": MODULE_API_VERSION,
            "unknown_enabled_modules": sorted(self.unknown_enabled_ids),
            "unknown_installed_modules": sorted(self.unknown_installed_ids),
            "unavailable_enabled_modules": sorted(self.unavailable_enabled_ids),
            "modules": [
                {
                    "id": record.id,
                    "version": record.spec.version if record.spec else None,
                    "status": record.status.value,
                    "required": record.spec.required if record.spec else False,
                    "dashboard_url": record.spec.dashboard_url if record.spec else None,
                    "error": record.error,
                    "component_errors": record.component_errors or {},
                }
                for record in self.records
            ],
        }

    @staticmethod
    def _load_object(path: str) -> Any:
        try:
            module_path, attribute = path.split(":", 1)
        except ValueError as exc:
            raise ValueError(f"Object path must use 'module:attribute' syntax: {path!r}") from exc
        module = importlib.import_module(module_path)
        return getattr(module, attribute)

    @staticmethod
    def _fail(record: ModuleRecord, component: str, exc: Exception) -> None:
        record.status = ModuleStatus.FAILED
        record.error = f"{component}: {type(exc).__name__}: {exc}"
        logger.error("Module %s %s failed: %s", record.id, component, exc)

    @staticmethod
    def _component_error(record: ModuleRecord, component: str, exc: Exception) -> None:
        if record.component_errors is None:
            record.component_errors = {}
        record.component_errors[component] = f"{type(exc).__name__}: {exc}"
        logger.error("Module %s %s capability failed: %s", record.id, component, exc)


def _configured_module_ids() -> set[str] | None:
    return load_enabled_module_ids()[0]


def _installed_module_ids() -> set[str] | None:
    settings = get_settings()
    configured = settings.INSTALLED_MODULES.strip()
    if not configured:
        marker = Path(settings.INSTALLED_MODULES_FILE)
        if marker.is_file():
            configured = marker.read_text().strip()
        elif settings.REQUIRE_INSTALLED_MODULES_MARKER:
            raise RuntimeError(f"Installed modules marker is required but missing: {marker}")
    if not configured and settings.REQUIRE_INSTALLED_MODULES_MARKER:
        raise RuntimeError("Installed modules marker is empty")
    if not configured or configured in {"*", "all"}:
        return None
    return {module_id.strip() for module_id in configured.split(",") if module_id.strip()}


module_registry = ModuleRegistry.discover(
    _configured_module_ids(),
    _installed_module_ids(),
    strict_enabled=True,
    strict_installed=True,
)
