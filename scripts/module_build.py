#!/usr/bin/env python3
"""Resolve reproducible image dependencies from a selected module set."""

import argparse
import importlib
import json
import os
import pkgutil
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def load_catalog(path: Path) -> dict:
    catalog = json.loads(path.read_text())
    if catalog.get("format_version") != 1 or not isinstance(catalog.get("modules"), dict):
        raise SystemExit(f"Unsupported module catalog: {path}")
    return catalog


def generate_catalog(project: Path) -> dict:
    sys.path.insert(0, str(project))
    import app.modules as modules_package

    optional_dependencies = tomllib.loads((project / "pyproject.toml").read_text())["project"].get(
        "optional-dependencies", {}
    )
    specs = []
    for _importer, package, is_package in pkgutil.iter_modules(
        modules_package.__path__, prefix="app.modules."
    ):
        if not is_package or package == "app.modules.system":
            continue
        spec = importlib.import_module(f"{package}.module").MODULE
        if spec.bundled:
            specs.append(spec)
    # System group lives one level deeper; ids are unchanged.
    try:
        import app.modules.system as system_package

        for _importer, package, is_package in pkgutil.iter_modules(
            system_package.__path__, prefix="app.modules.system."
        ):
            if not is_package:
                continue
            spec = importlib.import_module(f"{package}.module").MODULE
            if spec.bundled:
                specs.append(spec)
    except ImportError:
        pass

    modules = {}
    for spec in sorted(specs, key=lambda item: item.id):
        if spec.dependency_extra and spec.dependency_extra not in optional_dependencies:
            raise SystemExit(
                f"Module {spec.id!r} references missing optional dependency {spec.dependency_extra!r}"
            )
        modules[spec.id] = {
            "required": spec.required,
            "extra": spec.dependency_extra,
            "system_packages": list(spec.system_packages),
        }

    return {
        "format_version": 1,
        "default_modules": [
            spec.id
            for spec in sorted(specs, key=lambda item: (item.order, item.id))
            if spec.default_enabled and not spec.required
        ],
        "modules": modules,
    }


def write_catalog(path: Path, catalog: dict) -> None:
    path.write_text(json.dumps(catalog, indent=2, ensure_ascii=True) + "\n")


def resolve_modules(catalog: dict, selection: str) -> list[str]:
    available = catalog["modules"]
    requested = selection.strip()
    if requested in {"", "default"}:
        selected = set(catalog["default_modules"])
    elif requested == "core":
        selected = set()
    else:
        selected = {item.strip() for item in requested.split(",") if item.strip()}

    selected.update(module_id for module_id, data in available.items() if data.get("required"))
    unknown = selected - set(available)
    if unknown:
        raise SystemExit(f"Unknown modules: {', '.join(sorted(unknown))}")
    return [module_id for module_id in available if module_id in selected]


def dependency_extras(catalog: dict, modules: list[str]) -> list[str]:
    return sorted(
        {extra for module_id in modules if (extra := catalog["modules"][module_id].get("extra")) is not None}
    )


def system_packages(catalog: dict, modules: list[str]) -> list[str]:
    return sorted(
        {
            package
            for module_id in modules
            for package in catalog["modules"][module_id].get("system_packages", [])
        }
    )


def _normalize_module_id(raw_package: str) -> str:
    name = raw_package.strip().split("[", 1)[0].strip().lower().replace("-", "_")
    return name


_MODULE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def parse_external_modules(selection: str) -> list[tuple[str, str]]:
    """Parse ``NETSANCTUM_EXTERNAL_MODULES`` into ``(module_id, pip_spec)`` pairs.

    Each comma-separated item is either a plain module id / package name
    (``my_module`` or ``my-module``) implying ``pip install my_module``, or
    a PEP 508 requirement (``my_module @ git+https://...``) whose package
    name before ``@`` is the module id. The module id must equal the
    entry-point name declared by the external package.
    """
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in selection.split(","):
        spec = item.strip()
        if not spec:
            continue
        module_id = _normalize_module_id(spec.split("@", 1)[0])
        if not _MODULE_ID_PATTERN.fullmatch(module_id):
            raise SystemExit(f"Invalid external module id: {module_id!r} (from {spec!r})")
        if module_id in seen:
            raise SystemExit(f"Duplicate external module: {module_id!r}")
        seen.add(module_id)
        parsed.append((module_id, spec))
    return parsed


def resolve_external_modules(project: Path, selection: str) -> list[str]:
    del project  # kept for call-site compatibility; externals no longer need main pyproject extras
    return sorted(module_id for module_id, _spec in parse_external_modules(selection))


def sync_dependencies(args, catalog: dict, modules: list[str]) -> None:
    externals = parse_external_modules(args.external_modules or "")
    external_modules = sorted(module_id for module_id, _spec in externals)
    overlap = set(external_modules) & set(catalog["modules"])
    if overlap:
        raise SystemExit("External module IDs conflict with bundled modules: " + ", ".join(sorted(overlap)))
    command = [
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        "--no-install-project",
    ]
    for extra in dependency_extras(catalog, modules):
        command.extend(("--extra", extra))

    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(args.environment)
    subprocess.run(command, cwd=args.project, env=environment, check=True)
    if externals:
        pip_command = [
            "uv",
            "pip",
            "install",
            "--python",
            str(Path(args.environment) / "bin" / "python"),
            *[spec for _module_id, spec in externals],
        ]
        subprocess.run(pip_command, env=environment, check=True)
    args.marker.parent.mkdir(parents=True, exist_ok=True)
    args.marker.write_text(",".join([*modules, *external_modules]) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("catalog", "check", "modules", "extras", "system", "sync"),
    )
    parser.add_argument("--catalog", type=Path, default=Path("module-build.json"))
    parser.add_argument("--modules", default="default")
    parser.add_argument("--external-modules", default="")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--environment", type=Path, default=Path("/opt/venv"))
    parser.add_argument(
        "--marker",
        type=Path,
        default=Path("/opt/netsanctum/installed-modules"),
    )
    args = parser.parse_args()

    if args.command in {"catalog", "check"}:
        generated = generate_catalog(args.project)
        if args.command == "catalog":
            write_catalog(args.catalog, generated)
        elif not args.catalog.is_file() or load_catalog(args.catalog) != generated:
            raise SystemExit(f"{args.catalog} is stale; run scripts/module_build.py catalog")
        return

    catalog = load_catalog(args.catalog)
    modules = resolve_modules(catalog, args.modules)
    if args.command == "modules":
        print(",".join(modules))
    elif args.command == "extras":
        print(",".join(dependency_extras(catalog, modules)))
    elif args.command == "system":
        print(" ".join(system_packages(catalog, modules)))
    else:
        sync_dependencies(args, catalog, modules)


if __name__ == "__main__":
    main()
