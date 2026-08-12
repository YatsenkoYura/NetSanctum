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
├── __init__.py       # title, dashboard URL, display order
├── router.py         # FastAPI router
├── models.py         # optional SQLAlchemy models
├── schemas.py        # optional request/response schemas
├── tasks.py          # optional Celery jobs
├── requirements.in   # optional module dependencies
├── i18n.py           # optional translations
└── templates/        # optional UI
```

Minimal module metadata:

```python
TITLE_EN = "Example"
TITLE_RU = "Пример"
DASHBOARD_URL = "/example/dashboard"
ORDER = 50
```

Minimal router:

```python
from fastapi import APIRouter

router = APIRouter(prefix="/example", tags=["example"])


@router.get("/health")
async def health():
    return {"status": "ok"}
```

The core automatically mounts the exported `router`. Module-specific `requirements.in` files are also discovered and merged into the locked dependency set during the Docker build.

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

Some integrations are still coupled to existing modules. Extracting those integrations into stable core contracts is part of the framework roadmap.

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
ruff format --check .
ruff check .
```

Build and run regression tests in the runtime environment:

```bash
docker build -t netsanctum:test .
docker run --rm netsanctum:test python -m unittest discover -s tests -v
```

Apply database migrations:

```bash
alembic upgrade head
```

## Direction

The next framework-level milestones are:

- define explicit module lifecycle and service contracts
- remove remaining cross-module imports from product modules
- make modules independently enableable and removable
- provide module-owned migration registration
- add isolated module tests and health checks
- document a stable module authoring API
- support external modules without modifying the core repository

NetSanctum should remain useful as a personal server while these contracts are extracted from real modules rather than designed only in theory.
