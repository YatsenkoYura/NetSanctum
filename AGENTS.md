# NetSanctum Agent Notes

## Repository Shape

- This is a Python 3.12 modular monolith. `app/main.py` is the web entrypoint; Celery starts from `app.core.scheduler:celery_app`.
- Core infrastructure belongs in `app/core/`. Product behavior belongs in `app/modules/<id>/`; cross-module behavior must use the typed contracts/integration registry, not imports from another module's internals.
- Module discovery imports `app.modules.<package>.module:MODULE`; there is no central router list. `auth`, `settings`, and `sharing` are always required.
- System modules (`settings`, `storage`, `sharing`, ...) live one level deeper under `app/modules/system/` but keep flat module ids; the `system` container itself holds no `MODULE` and is skipped, not failed.
- Image installation and runtime activation are separate: `NETSANCTUM_MODULES` selects build dependencies and the installed-module marker, while `ENABLED_MODULES` may only disable installed optional modules. A non-empty `ENABLED_MODULES` env value overrides the dashboard-persisted `enabled-modules.json`. Installed disabled modules are still migrated.

## Verification

- Install the exact development environment with `uv sync --locked --all-extras`; run Python tools through `uv run`.
- Tests use `unittest`, not pytest. Focus a test with `uv run python -m unittest tests.test_module_contracts.ModuleManifestTests.test_committed_module_build_catalog_matches_manifests -v` or a file with `uv run python -m unittest tests.test_module_contracts -v`.
- Fast repository checks are `uv run ruff format --check .`, `uv run ruff check .`, and `uv run pre-commit run --all-files`.
- The authoritative full regression run includes image-only system dependencies: `docker build -t netsanctum:test .` then `docker run --rm netsanctum:test python -m unittest discover -s tests -v`.
- `tests.test_migrations.PostgresMigrationSmokeTests` only runs with `MIGRATION_TEST_DATABASE_URL`; it drops and recreates `public`, and the URL's database name must end in `_test`.

## Coupled Changes

- After changing a bundled module's manifest, dependency extra, or system packages, run `uv lock` when dependencies changed, then `uv run python scripts/module_build.py catalog` and `uv run python scripts/module_build.py check`. `module-build.json` is generated and committed.
- Template or Python-generated Tailwind class changes require `npm ci && npm run build:css`; commit the resulting `static/tailwind.css`. Tailwind scans `.html` and `.py` under `app/` plus `static/browser-runtime.js`, `static/miku-assistant.js`, and `static/miku-dashboard.js`.
- Database-backed modules own independent Alembic histories under their package. Use `uv run python -m app.core.migrations revision <module> -m "..."`, then `upgrade <module>` and `check <module>`; do not use the root Alembic CLI for new module migrations.
- A migration manifest's `tables` must exactly match that module's current model tables. Move removed table names to `historical_tables`; table ownership is permanent and cannot be reassigned to another module.

## Runtime Cautions

- Prefer Docker Compose for the real runtime: web, worker, PostgreSQL, Redis, migration, browser, and MIKU processes have distinct environments and network access.
- `./start.sh` is not a read-only verification command: it creates/updates `.env`, generates secrets, may change `HOST_PORT`, and rebuilds/starts Compose services.
- Configuration loads `.env` at import time unless `NETSANCTUM_LOAD_DOTENV=0`, and `get_settings()` is process-cached. Set environment overrides before importing application modules in tests or scripts.

## Vault media

- A sealed collection's file key is derived per collection from the session's inbox private key. Any endpoint that reads a sealed file must resolve that key itself and refuse (423) when it cannot — never let the reader fall through to the application key, which opens nothing and aborts the response mid-stream.
- `<video>` cannot send the `X-Vault-Unlock` header, so a signed media URL is accompanied by a key grant in Redis (`store_media_key_grant`/`load_media_key_grant`, keyed by the URL signature, same 15-minute TTL). A new endpoint that serves sealed bytes to a player needs the same pairing; `tests/test_vault_file_keys.py::PlayerStreamingTests` covers the path.
- A video captured into a sealed vault lands as a blind write (its own item key, wrapped under the collection's inbox public key) and is re-sealed by `finalize_blind_media_task`, queued automatically on the next unlock. Blind rows must stay unplayable and loudly refused, never weakly stored.

## Vault card types

- `node_type` is a free-form `String` and deliberately structural: it is not in `SEALED_FIELDS`, because a locked vault still has to lay out its grid. Its vocabulary, the view each type opens, and the capture kinds the extension may push all live in `app/modules/vault/node_types.py`; nothing else may spell a type as a bare string or `Literal`.
- The view table reaches the template through `app/core/template_globals.py`, not a route's context. The dashboard is rendered by two callers — its own route and the sharing module as a read-only page — so a context value exists on only one of them. A module must never import `app.core.templates` at import time: it imports the registry that discovery is in the middle of importing, and the module then drops out of the registry silently. Publish from a module that imports nothing, as `template_globals.py` does.
- `app/modules/vault/local_types/` is git-ignored: an owner's own card types live there as one `VIEWS` dict per file, so adding a type costs no committed change. Read through `node_types.local_views`, which treats an absent directory as no local types, and warns rather than fails. Local types are snapshotted at import, so a new file needs a process restart.
