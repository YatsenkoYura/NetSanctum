"""Stable declarative types available to module manifests."""

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

MODULE_API_VERSION = 1
MODULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
INTEGRATION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*\.v[1-9][0-9]*$")
UI_EXTENSION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
MIGRATION_REVISION_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TABLE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
SHARE_PATH_PARAMETER_PATTERN = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")


def _validate_object_path(path: str, label: str) -> None:
    module_path, separator, attribute = path.partition(":")
    if not separator or not module_path or not attribute:
        raise ValueError(f"{label} must use 'module:attribute' syntax: {path!r}")


@dataclass(frozen=True, slots=True)
class IntegrationSpec:
    """A versioned operation implemented by a module."""

    id: str
    handler: str
    request_model: str
    result_model: str
    contract: str | None = None
    resource_handler: str | None = None
    resource_request_model: str | None = None

    def __post_init__(self) -> None:
        if not INTEGRATION_ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid versioned integration id: {self.id!r}")
        _validate_object_path(self.handler, "Integration handler")
        _validate_object_path(self.request_model, "Integration request model")
        _validate_object_path(self.result_model, "Integration result model")
        if self.contract and not INTEGRATION_ID_PATTERN.fullmatch(self.contract):
            raise ValueError(f"Invalid versioned integration contract: {self.contract!r}")
        if bool(self.resource_handler) != bool(self.resource_request_model):
            raise ValueError("Integration resource handler and request model must be declared together")
        if self.resource_handler:
            assert self.resource_request_model is not None
            _validate_object_path(self.resource_handler, "Integration resource handler")
            _validate_object_path(self.resource_request_model, "Integration resource request model")


@dataclass(frozen=True, slots=True)
class UiActionSpec:
    """Structured UI contribution that invokes an integration."""

    id: str
    slot: str
    integration: str
    label_en: str
    label_ru: str
    entity_types: tuple[str, ...] = ()
    order: int = 100

    def __post_init__(self) -> None:
        if not UI_EXTENSION_ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid UI action id: {self.id!r}")
        if not UI_EXTENSION_ID_PATTERN.fullmatch(self.slot):
            raise ValueError(f"Invalid UI action slot: {self.slot!r}")
        if not INTEGRATION_ID_PATTERN.fullmatch(self.integration):
            raise ValueError(f"Invalid UI action integration id: {self.integration!r}")
        if not self.label_en.strip() or not self.label_ru.strip():
            raise ValueError(f"UI action {self.id!r} must declare English and Russian labels")


@dataclass(frozen=True, slots=True)
class IntegrationContext:
    """Request-scoped infrastructure passed to an integration handler."""

    session: Any
    user: Any
    registry: Any
    consumer_id: str | None = None


@dataclass(frozen=True, slots=True)
class IntegrationResource:
    """Internal resource resolved by a provider without exposing its storage path over JSON."""

    kind: str
    title: str
    storage_path: str | None = None
    text: str | None = None
    subtitle: str | None = None
    duration: float = 0
    page: int | None = None
    pages_count: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"audio", "video", "image", "text"}:
            raise ValueError(f"Unsupported integration resource kind: {self.kind!r}")
        if self.kind == "text" and self.text is None:
            raise ValueError("Text integration resource must provide text")
        if self.kind != "text" and not self.storage_path:
            raise ValueError("Media integration resource must provide a storage path")
        if self.duration < 0:
            raise ValueError("Integration resource duration cannot be negative")


class IntegrationUnavailableError(LookupError):
    """Raised when no active module provides an integration."""


class IntegrationRejectedError(ValueError):
    """Raised when an integration cannot handle the supplied context."""


class IntegrationNotFoundError(LookupError):
    """Raised when an integration target does not exist."""


@dataclass(frozen=True, slots=True)
class MigrationSpec:
    """Module-owned Alembic history and its immutable legacy boundary."""

    path: str
    baseline_revision: str
    tables: tuple[str, ...]
    legacy_tables: tuple[str, ...] = ()
    historical_tables: tuple[str, ...] = ()

    @property
    def owned_tables(self) -> tuple[str, ...]:
        return (*self.tables, *self.historical_tables)

    def __post_init__(self) -> None:
        migration_path = PurePosixPath(self.path)
        if (
            self.path != self.path.strip()
            or not self.path
            or migration_path.is_absolute()
            or ".." in migration_path.parts
            or "\\" in self.path
        ):
            raise ValueError(f"Migration path must be package-relative: {self.path!r}")
        if not MIGRATION_REVISION_PATTERN.fullmatch(self.baseline_revision):
            raise ValueError(f"Invalid migration baseline revision: {self.baseline_revision!r}")
        if not self.tables:
            raise ValueError("Module migrations must declare owned tables")
        if len(self.owned_tables) != len(set(self.owned_tables)):
            raise ValueError("Module migrations declare duplicate tables")
        for table in self.owned_tables:
            if not TABLE_NAME_PATTERN.fullmatch(table):
                raise ValueError(f"Invalid owned table name: {table!r}")
            if table == "alembic_version" or table.startswith("alembic_version_"):
                raise ValueError(f"Migration table name is reserved: {table!r}")
            if table == "netsanctum_migration_ownership":
                raise ValueError(f"Migration table name is reserved: {table!r}")
        if len(self.legacy_tables) != len(set(self.legacy_tables)):
            raise ValueError("Module migrations declare duplicate legacy tables")
        unknown_legacy_tables = set(self.legacy_tables) - set(self.owned_tables)
        if unknown_legacy_tables:
            raise ValueError(
                "Legacy tables must be owned by the module: " + ", ".join(sorted(unknown_legacy_tables))
            )


def _validate_share_path(path: str, label: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or path != path.strip("/")
        or parsed.is_absolute()
        or ".." in parsed.parts
        or "?" in path
        or "#" in path
        or "\\" in path
    ):
        raise ValueError(f"{label} must be a relative URL path: {path!r}")
    for part in parsed.parts:
        if "{" in part or "}" in part:
            if not SHARE_PATH_PARAMETER_PATTERN.fullmatch(part):
                raise ValueError(f"{label} has an invalid path parameter: {path!r}")


@dataclass(frozen=True, slots=True)
class ShareRoute:
    """Read-only API route exposed inside a scoped share."""

    name: str
    path: str
    source: str = "entities"

    def __post_init__(self) -> None:
        if not UI_EXTENSION_ID_PATTERN.fullmatch(self.name):
            raise ValueError(f"Invalid share route name: {self.name!r}")
        _validate_share_path(self.path, "Share route")
        if self.source not in {"entities", "relations"}:
            raise ValueError(f"Invalid share route source: {self.source!r}")


@dataclass(frozen=True, slots=True)
class ShareAsset:
    """Binary or text asset exposed inside a scoped share."""

    name: str
    path: str

    def __post_init__(self) -> None:
        if not UI_EXTENSION_ID_PATTERN.fullmatch(self.name):
            raise ValueError(f"Invalid share asset name: {self.name!r}")
        _validate_share_path(self.path, "Share asset")


@dataclass(frozen=True, slots=True)
class ShareSpec:
    """Declarative read-only sharing contract implemented by a module."""

    provider: str
    selector_key: str
    dashboard_template: str
    api_prefix: str
    routes: tuple[ShareRoute, ...] = ()
    assets: tuple[ShareAsset, ...] = ()
    max_items: int = 500

    def __post_init__(self) -> None:
        _validate_object_path(self.provider, "Share provider")
        if not TABLE_NAME_PATTERN.fullmatch(self.selector_key):
            raise ValueError(f"Invalid share selector key: {self.selector_key!r}")
        template_path = PurePosixPath(self.dashboard_template)
        if (
            not self.dashboard_template.endswith(".html")
            or template_path.is_absolute()
            or ".." in template_path.parts
            or "\\" in self.dashboard_template
        ):
            raise ValueError(
                f"Share dashboard template must be package-relative: {self.dashboard_template!r}"
            )
        if not self.api_prefix.startswith("/api/") or self.api_prefix.endswith("/"):
            raise ValueError("Share API prefix must start with '/api/' and have no trailing slash")
        if self.max_items < 1:
            raise ValueError("Share max_items must be positive")
        route_names = [route.name for route in self.routes]
        route_paths = [route.path for route in self.routes]
        asset_names = [asset.name for asset in self.assets]
        asset_paths = [asset.path for asset in self.assets]
        if len(route_names) != len(set(route_names)) or len(route_paths) != len(set(route_paths)):
            raise ValueError("Share routes must have unique names and paths")
        if len(asset_names) != len(set(asset_names)) or len(asset_paths) != len(set(asset_paths)):
            raise ValueError("Share assets must have unique names and paths")
        if set(route_paths) & set(asset_paths):
            raise ValueError("Share routes and assets must not use the same path")


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """Declarative contract exported by an installed NetSanctum module."""

    id: str
    version: str
    title_en: str
    title_ru: str
    dashboard_url: str | None = None
    order: int = 100
    router: str | None = None
    models: str | None = None
    tasks: str | None = None
    templates: str | None = None
    i18n: str | None = None
    startup: str | None = None
    shutdown: str | None = None
    file_cleanup: str | None = None
    module_cleanup: str | None = None
    storage_namespaces: tuple[str, ...] = ()
    package_prefixes: tuple[str, ...] = ()
    package_resolver: str | None = None
    share: ShareSpec | None = None
    entity_types: tuple[str, ...] = ()
    entity_resolver: str | None = None
    migrations: MigrationSpec | None = None
    integrations: tuple[IntegrationSpec, ...] = ()
    uses_integrations: tuple[str, ...] = ()
    uses_integration_contracts: tuple[str, ...] = ()
    ui_actions: tuple[UiActionSpec, ...] = ()
    progress_key_patterns: tuple[str, ...] = ()
    dependency_extra: str | None = None
    system_packages: tuple[str, ...] = ()
    bundled: bool = True
    default_enabled: bool = True
    required: bool = False
    api_version: int = MODULE_API_VERSION

    def __post_init__(self) -> None:
        if not MODULE_ID_PATTERN.fullmatch(self.id):
            raise ValueError(f"Invalid module id: {self.id!r}")
        if not self.version.strip():
            raise ValueError(f"Module {self.id!r} must declare a version")
        if self.dashboard_url and not self.dashboard_url.startswith("/"):
            raise ValueError(f"Module {self.id!r} dashboard_url must start with '/'")
        if bool(self.package_prefixes) != bool(self.package_resolver):
            raise ValueError(
                f"Module {self.id!r} must declare package_prefixes and package_resolver together"
            )
        if self.share and not self.templates:
            raise ValueError(f"Module {self.id!r} sharing requires templates")
        if bool(self.entity_types) != bool(self.entity_resolver):
            raise ValueError(f"Module {self.id!r} must declare entity_types and entity_resolver together")
        if (self.file_cleanup or self.module_cleanup) and not self.storage_namespaces:
            raise ValueError(f"Module {self.id!r} cleanup hooks require storage_namespaces")
        if self.migrations and not self.models:
            raise ValueError(f"Module {self.id!r} migrations require a models module")
        if self.migrations and len(f"alembic_version_{self.id}") > 63:
            raise ValueError(f"Module {self.id!r} is too long for a PostgreSQL migration version table")
        integration_ids = [integration.id for integration in self.integrations]
        if len(integration_ids) != len(set(integration_ids)):
            raise ValueError(f"Module {self.id!r} declares duplicate integrations")
        for integration_id in self.uses_integrations:
            if not INTEGRATION_ID_PATTERN.fullmatch(integration_id):
                raise ValueError(f"Invalid used integration id: {integration_id!r}")
        if len(self.uses_integrations) != len(set(self.uses_integrations)):
            raise ValueError(f"Module {self.id!r} declares duplicate integration uses")
        for contract in self.uses_integration_contracts:
            if not INTEGRATION_ID_PATTERN.fullmatch(contract):
                raise ValueError(f"Invalid used integration contract: {contract!r}")
        if len(self.uses_integration_contracts) != len(set(self.uses_integration_contracts)):
            raise ValueError(f"Module {self.id!r} declares duplicate integration contract uses")
        action_ids = [action.id for action in self.ui_actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError(f"Module {self.id!r} declares duplicate UI actions")
