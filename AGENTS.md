# NetSanctum Agent Notes

## Repository Shape

- This is a Python 3.12 modular monolith. `app/main.py` is the web entrypoint; Celery starts from `app.core.scheduler:celery_app`.
- Core infrastructure belongs in `app/core/`. Product behavior belongs in `app/modules/<id>/`; cross-module behavior must use the typed contracts/integration registry, not imports from another module's internals.
- Module discovery imports `app.modules.<package>.module:MODULE`; there is no central router list. `auth`, `settings`, and `sharing` are always required.
- Image installation and runtime activation are separate: `NETSANCTUM_MODULES` selects build dependencies and the installed-module marker, while `ENABLED_MODULES` may only disable installed optional modules. Installed disabled modules are still migrated.

## Verification

- Install the exact development environment with `uv sync --locked --all-extras`; run Python tools through `uv run`.
- Tests use `unittest`, not pytest. Focus a test with `uv run python -m unittest tests.test_module_contracts.ModuleManifestTests.test_committed_module_build_catalog_matches_manifests -v` or a file with `uv run python -m unittest tests.test_module_contracts -v`.
- Fast repository checks are `uv run ruff format --check .`, `uv run ruff check .`, and `uv run pre-commit run --all-files`.
- The authoritative full regression run includes image-only system dependencies: `docker build -t netsanctum:test .` then `docker run --rm netsanctum:test python -m unittest discover -s tests -v`.
- `tests.test_migrations.PostgresMigrationSmokeTests` only runs with `MIGRATION_TEST_DATABASE_URL`; it drops and recreates `public`, and the URL's database name must end in `_test`.

## Coupled Changes

- After changing a bundled module's manifest, dependency extra, or system packages, run `uv lock` when dependencies changed, then `uv run python scripts/module_build.py catalog` and `uv run python scripts/module_build.py check`. `module-build.json` is generated and committed.
- Template or Python-generated Tailwind class changes require `npm ci && npm run build:css`; commit the resulting `static/tailwind.css`. Tailwind scans `.html` and `.py` under `app/` plus `static/browser-runtime.js` and `static/miku-assistant.js`.
- Database-backed modules own independent Alembic histories under their package. Use `uv run python -m app.core.migrations revision <module> -m "..."`, then `upgrade <module>` and `check <module>`; do not use the root Alembic CLI for new module migrations.
- A migration manifest's `tables` must exactly match that module's current model tables. Move removed table names to `historical_tables`; table ownership is permanent and cannot be reassigned to another module.

## Runtime Cautions

- Prefer Docker Compose for the real runtime: web, worker, PostgreSQL, Redis, migration, browser, and MIKU processes have distinct environments and network access.
- `./start.sh` is not a read-only verification command: it creates/updates `.env`, generates secrets, may change `HOST_PORT`, and rebuilds/starts Compose services.
- Configuration loads `.env` at import time unless `NETSANCTUM_LOAD_DOTENV=0`, and `get_settings()` is process-cached. Set environment overrides before importing application modules in tests or scripts.
