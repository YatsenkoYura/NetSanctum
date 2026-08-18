# NetSanctum

NetSanctum is a single-user, self-hosted platform for independent web modules.

It is a modular monolith today and is gradually evolving into a small application framework: the core provides shared infrastructure, while modules own their routes, data, background jobs, dependencies, templates, and user experience.

The goal is not to build one application with an endless list of features. The goal is to provide one private runtime where focused applications can be installed and developed without turning the core into a collection of feature-specific code.

> NetSanctum is under active development. Interfaces and module contracts may still change.

## Core Idea

```text
NetSanctum core
├── authentication
├── scoped module sharing
├── database sessions and migrations
├── background task runtime
├── local and S3 storage
├── encrypted file storage
├── templates and localization
└── module discovery
    ├── AllLib
    ├── Music
    ├── Vault
    ├── Video Archiver
    └── future independent modules
```

The core discovers packages under `app/modules/` at startup. A module can expose metadata and a FastAPI router without being registered manually in the main application.

Modules should communicate through small core contracts instead of depending on each other's internal implementation. A module should remain removable without breaking unrelated modules.

## Module Contract

A module lives in `app/modules/<module_name>/`:

```text
app/modules/example/
├── __init__.py       # exports the module manifest
├── module.py         # declarative ModuleSpec manifest
├── router.py         # FastAPI router
├── models.py         # optional SQLAlchemy models
├── schemas.py        # optional request/response schemas
├── tasks.py          # optional Celery jobs
├── i18n.py           # optional translations
└── templates/        # optional UI
```

Minimal module manifest:

```python
from app.core.module_types import ModuleSpec

MODULE = ModuleSpec(
    id="example",
    version="0.1.0",
    title_en="Example",
    title_ru="Пример",
    dashboard_url="/example/dashboard",
    order=50,
    router="app.modules.example.router:router",
    dependency_extra="example",
    system_packages=("ffmpeg",),
)
```

Minimal router:

```python
from fastapi import APIRouter

router = APIRouter(prefix="/example", tags=["example"])


@router.get("/health")
async def health():
    return {"status": "ok"}
```

The core automatically mounts the exported `router`. Python dependencies live in a matching `pyproject.toml` optional extra and are resolved once in the committed `uv.lock`. System dependencies are declared by `system_packages`.

`NETSANCTUM_MODULES` selects which module dependencies are installed into an image. `ENABLED_MODULES` is an optional runtime allowlist and must be a subset of that installed set. `auth` and `settings` are required and remain enabled. Disabled modules are not mounted or started, but their database schema is retained.

Authenticated module diagnostics are available from `GET /api/modules`.

## Control Center

The owner-only `/dashboard` is the runtime control plane:

- PostgreSQL, Redis, Celery worker, module, and process readiness
- installed, active, disabled, unavailable, and failed module states
- persisted desired module state, applied after web and worker restart
- Redis task trackers with per-task cancellation
- bounded, redacted web and worker logs retained in Redis
- global, module, and user-scoped settings with typed and secret values

The application does not mount the Docker socket. Container restart and host-level operations remain explicit deployment actions.

External Python packages can register a trusted server-side module through an entry point:

```toml
[project.entry-points."netsanctum.modules"]
example = "example_netsanctum.module:MODULE"
```

Add a pure-Python package to a `pyproject.toml` optional extra whose name matches the module ID, update `uv.lock`, and build with `NETSANCTUM_EXTERNAL_MODULES=example`. The build installs that extra and records the ID in the image marker. Entry points absent from the marker are not imported, including when the marker itself is missing.

External modules that require operating-system packages must provide a derived Dockerfile that installs them explicitly. NetSanctum intentionally does not install arbitrary system packages from third-party metadata and never runs `pip`, `uv`, or `apt` dynamically at application startup.

## Shared Infrastructure

Modules can use:

- FastAPI and shared single-user authentication
- asynchronous SQLAlchemy sessions for HTTP handlers
- synchronous SQLAlchemy sessions for Celery workers
- Redis-backed sessions and task progress
- Celery background jobs
- local or S3-compatible object storage
- optional AES-GCM file encryption
- Jinja and HTMX-based server-rendered interfaces
- shared settings and localization
- ranged media responses for audio and video

The intended boundary is simple: the core owns infrastructure; modules own product behavior.

## Current Modules

- **AllLib** downloads and reads novels, manga, and anime from supported Lib-network sources.
- **Music** archives audio, organizes playlists, and serves a local player.
- **Video Archiver** downloads and streams videos, subtitles, metadata, and comments.
- **Vault** stores notes, bookmarks, collections, ratings, and media progress.
- **Storage Manager** displays storage usage and performs module-aware cleanup.
- **Auth and Settings** provide internal platform services used by the other modules.
- **Sharing** publishes an isolated, read-only module view with optional content selection, password, and expiry.

Cross-module behavior uses explicit capabilities registered through the module manifest.

### Module sharing

Modules opt into isolated read-only sharing through a declarative manifest contract. Core owns link
authentication, expiry, dashboard rendering, URL scoping, route matching, and the deny-by-default
mutation policy. A provider implements only module-specific catalog, selection, entity, relation, and
asset handlers:

```python
from app.core.module_types import ShareAsset, ShareRoute, ShareSpec

MODULE = ModuleSpec(
    # ...regular module metadata...
    share=ShareSpec(
        provider="example.share:PROVIDER",
        selector_key="item_ids",
        dashboard_template="example_dashboard.html",
        api_prefix="/api/example",
        routes=(
            ShareRoute(name="items", path="items"),
            ShareRoute(name="item", path="items/{item_id}"),
        ),
        assets=(
            ShareAsset(name="file", path="items/{item_id}/file"),
        ),
    ),
)
```

Shared templates reuse the owner dashboard with `{% extends module_base|default("base.html") %}`.
Only declared GET/HEAD routes are dispatched; all unsafe methods and undeclared paths are rejected by
the framework before provider code runs.

### Module integrations

Modules expose versioned operations through the central registry instead of importing or calling one
another directly. A provider declares a typed handler and optional UI contribution:

```python
from app.core.module_types import IntegrationSpec, ModuleSpec, UiActionSpec

MUSIC_MODULE = ModuleSpec(
    # ...regular module metadata...
    integrations=(
        IntegrationSpec(
            id="media.audio.import.v1",
            handler="example.integrations:import_audio",
            request_model="example.integrations:ImportRequest",
            result_model="example.integrations:ImportResult",
        ),
    ),
    ui_actions=(
        UiActionSpec(
            id="example.import_audio",
            slot="entity.actions",
            integration="media.audio.import.v1",
            label_en="Import audio",
            label_ru="Импортировать аудио",
            entity_types=("video",),
        ),
    ),
)
```

The entity-owning module explicitly allows the action without importing its provider:

```python
VIDEO_MODULE = ModuleSpec(
    # ...regular module metadata and entity resolver...
    uses_integrations=("media.audio.import.v1",),
)
```

Handlers accept their declared Pydantic request model and an `IntegrationContext`, then return the
declared result model. `GET /api/integrations` exposes active contracts and JSON schemas;
`POST /api/integrations/{integration_id}` invokes them. UI pages provide generic
`<netsanctum-actions>` slots, and the framework renders only actions whose provider is active and whose
contract the entity-owning module declared in `uses_integrations`. UI contributions contain structured
metadata only, never arbitrary HTML or JavaScript.

## Quick Start

Requirements:

- Docker
- Docker Compose

```bash
./start.sh
```

Open `http://localhost:8000` by default.

The default Compose port is bound to `127.0.0.1`. For remote sharing, place NetSanctum behind an
HTTPS reverse proxy or VPN, set `TRUSTED_HOSTS` and `PUBLIC_BASE_URL`, and enable `SECURE_COOKIES`.

On the first start, NetSanctum creates `storage/config/access_token.txt`. Use the token to sign in, store it safely, and remove the plaintext file afterward. Owner credentials are accepted only through the login form, session cookie, or `Authorization: Bearer`; query-string credentials are intentionally rejected.

Management commands:

```bash
./start.sh --logs
./start.sh --restart
./start.sh --down
```

Use `./start.sh 4000` or `./start.sh -p 4000` to select another host port.

## Development

Rebuild the committed Tailwind stylesheet after changing template classes:

```bash
npm ci
npm run build:css
```

NetSanctum serves precompiled CSS and does not run the Tailwind browser compiler. Bundled templates and Python-generated HTML are scanned by `tailwind.config.js`. External modules must ship their own compiled styles for classes outside the core stylesheet.

Install the repository hooks once per clone:

```bash
uv sync --locked --all-extras
uv run pre-commit install
```

The hooks apply Ruff fixes and formatting, validate locks and module metadata, run lightweight contract tests, and rebuild the committed Tailwind stylesheet when module templates change. Run the complete hook set manually with `uv run pre-commit run --all-files`.

Run static checks:

```bash
uv sync --locked --all-extras
uv run ruff format --check .
uv run ruff check .
```

Build and run regression tests in the runtime environment:

```bash
docker build -t netsanctum:test .
docker run --rm netsanctum:test python -m unittest discover -s tests -v
```

Build a smaller image with only selected modules:

```bash
docker build --build-arg NETSANCTUM_MODULES=core -t netsanctum:core .
docker build --build-arg NETSANCTUM_MODULES=storage,vault -t netsanctum:vault .
```

After changing module dependency metadata:

```bash
uv lock
python scripts/module_build.py catalog
python scripts/module_build.py check
```

Apply database migrations:

```bash
python -m app.core.migrations upgrade
```

Each database-backed module owns an independent Alembic history and version table under its package.
The runner upgrades installed modules, including disabled ones so their data remains compatible. Existing
databases using the legacy global history are upgraded and adopted automatically; no manual `stamp` is
required.

Database-backed module manifests declare their migration directory and table ownership:

```python
migrations=MigrationSpec(
    path="migrations",
    baseline_revision="example_0001",
    tables=("example_items",),
    legacy_tables=(),
)
```

`legacy_tables` is only for bundled schemas that existed in the former global migration history.
`tables` is the module's permanent ownership namespace; names of removed tables move to
`historical_tables` so another module cannot reuse them.

Create a migration for one module:

```bash
python -m app.core.migrations revision music -m "add album field"
python -m app.core.migrations upgrade music
python -m app.core.migrations check music
```

External module authors must point revision generation at their writable source tree instead of the
installed wheel:

```bash
python -m app.core.migrations revision example -m "add field" \
  --version-path ./example_netsanctum/migrations/versions
```

## Direction

The next framework-level milestones are:

- add isolated module tests and health checks
- document a stable module authoring API
- support external modules without modifying the core repository

NetSanctum should remain useful as a personal server while these contracts are extracted from real modules rather than designed only in theory.
