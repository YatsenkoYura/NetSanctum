# NetSanctum

NetSanctum is a single-user, self-hosted platform for independent web modules.

It is a modular monolith today and is gradually evolving into a small application framework: the core provides shared infrastructure, while modules own their routes, data, background jobs, dependencies, templates, and user experience.

The goal is not to build one application with an endless list of features. The goal is to provide one private runtime where focused applications can be installed and developed without turning the core into a collection of feature-specific code.

> NetSanctum is under active development. Interfaces and module contracts may still change.

## Core Idea

```text
NetSanctum core
├── authentication
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

Cross-module behavior uses explicit capabilities registered through the module manifest.

## Quick Start

Requirements:

- Docker
- Docker Compose

```bash
cp .env.example .env
./start.sh
```

Open `http://localhost:8000` by default.

On the first start, NetSanctum creates `access_token.txt`. Use the token to sign in, store it safely, and remove the plaintext file afterward.

Management commands:

```bash
./start.sh --logs
./start.sh --restart
./start.sh --down
```

Use `./start.sh 4000` or `./start.sh -p 4000` to select another host port.

## Development

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
alembic upgrade head
```

## Direction

The next framework-level milestones are:

- move module migrations into module-owned branches
- add isolated module tests and health checks
- document a stable module authoring API
- support external modules without modifying the core repository

NetSanctum should remain useful as a personal server while these contracts are extracted from real modules rather than designed only in theory.
