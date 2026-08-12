#!/usr/bin/env python3
"""Resolve reproducible image dependencies from a selected module set."""

import argparse
import importlib
import json
import os
import pkgutil
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
        if not is_package:
            continue
        spec = importlib.import_module(f"{package}.module").MODULE
        if spec.bundled:
            specs.append(spec)

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


def resolve_external_modules(project: Path, selection: str) -> list[str]:
    selected = sorted({item.strip() for item in selection.split(",") if item.strip()})
    if not selected:
        return []
    optional_dependencies = tomllib.loads((project / "pyproject.toml").read_text())["project"].get(
        "optional-dependencies", {}
    )
    unknown = set(selected) - set(optional_dependencies)
    if unknown:
        raise SystemExit(
            "External module IDs must have matching pyproject extras: " + ", ".join(sorted(unknown))
        )
    return selected


def sync_dependencies(args, catalog: dict, modules: list[str]) -> None:
    external_modules = resolve_external_modules(args.project, args.external_modules)
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
    for extra in sorted(set(dependency_extras(catalog, modules)) | set(external_modules)):
        command.extend(("--extra", extra))

    environment = os.environ.copy()
    environment["UV_PROJECT_ENVIRONMENT"] = str(args.environment)
    subprocess.run(command, cwd=args.project, env=environment, check=True)
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
