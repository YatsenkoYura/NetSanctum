"""Stable declarative types available to module manifests."""

import re
from dataclasses import dataclass

MODULE_API_VERSION = 1
MODULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


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
    entity_types: tuple[str, ...] = ()
    entity_resolver: str | None = None
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
        if bool(self.entity_types) != bool(self.entity_resolver):
            raise ValueError(f"Module {self.id!r} must declare entity_types and entity_resolver together")
        if (self.file_cleanup or self.module_cleanup) and not self.storage_namespaces:
            raise ValueError(f"Module {self.id!r} cleanup hooks require storage_namespaces")
