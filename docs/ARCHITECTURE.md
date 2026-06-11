# NetSanctum: Modular Monolith Architecture

NetSanctum is built as a single-user, self-hosted **Modular Monolith**. Every feature (such as YouTube Video Archiver or RanobeLib Downloader) is isolated into its own independent module package. The core engine dynamically discovers, mounts, and configures these modules at runtime.

---

## 1. Directory Structure

A NetSanctum module lives in `app/modules/<module_name>/`. A fully featured module has the following layout:

```text
app/modules/my_module/
├── __init__.py         # Module metadata (titles, dashboard URL, order)
├── models.py           # SQLAlchemy database schemas
├── router.py           # FastAPI APIRouter (exposes `router` object)
├── tasks.py            # Celery background tasks
├── requirements.in     # Module-specific Python dependencies
└── templates/          # HTML templates / HTMX fragments
    └── my_dashboard.html
```

---

## 2. Dynamic Discovery & Mounting

The core engine in `app/main.py` performs dynamic registration at startup:

### 2.1 Router Registration
The engine scans `app.modules.*` using `pkgutil.iter_modules`. For each package:
1. It imports `<module_name>.router`.
2. If the module has a variable named `router` (FastAPI `APIRouter`), it mounts it to the FastAPI application.
3. It extracts `TITLE_EN`, `TITLE_RU`, `DASHBOARD_URL`, and `ORDER` from the module's `__init__.py` to dynamically construct the dashboard sidebar tabs.

### 2.2 Schema Discovery
During the `lifespan` startup event, the engine:
1. Dynamically imports `<module_name>.models` to register SQLAlchemy classes in metadata.
2. Runs `Base.metadata.create_all` to automatically create or verify database tables.

### 2.3 Dependency Compilation
The Docker build pipeline dynamically finds all `requirements.in` files:
```dockerfile
RUN uv pip compile requirements.in $(find app/modules -name requirements.in) --generate-hashes --python 3.12 -o requirements.txt
```
This merges core and module-specific requirements into a single locked file with cryptographic hashes.

---

## 3. Core Shared Utilities

Modules must not use raw filesystem functions or implement custom DB connectors. Instead, they must import shared core components:

### 3.1 Database Access
Use standard SQLAlchemy asynchronous sessions:
```python
from app.core.database import get_db, AsyncSession
from fastapi import Depends

@router.get("/api/data")
async def get_data(db: AsyncSession = Depends(get_db)):
    # ... query logic ...
```

### 3.2 Unified Storage Interface
The storage layer handles file reads, writes, and encryption seamlessly, whether using local disk or S3:
```python
from app.core.storage import get_storage

storage = get_storage()

# Regular save
storage.save_file(binary_data, "folder/file.jpg")

# Encrypted save (AES-256-GCM)
storage.save_file_encrypted(private_data, "vault/data.enc")

# Decrypted read
data = storage.get_file_decrypted("vault/data.enc")
```

### 3.3 Task Scheduling (Celery)
To offload heavy/long-running operations, define tasks using the central Celery app:
```python
from app.core.scheduler import celery_app

@celery_app.task
def long_running_task(param):
    # logic
```

---

## 4. Steps to Create a New Module

1. Create `app/modules/my_module/`.
2. Declare metadata in `app/modules/my_module/__init__.py`:
   ```python
   TITLE_EN = "My Module"
   TITLE_RU = "Мой Модуль"
   DASHBOARD_URL = "/my-module/dashboard"
   ORDER = 30
   ```
3. Create `app/modules/my_module/models.py` (optional):
   ```python
   from app.core.database import Base
   from sqlalchemy import Column, Integer, String

   class MyModel(Base):
       __tablename__ = "my_table"
       id = Column(Integer, primary_key=True)
       # ...
   ```
4. Create `app/modules/my_module/router.py` containing:
   ```python
   from fastapi import APIRouter
   router = APIRouter()
   ```
5. If the module requires extra pip packages, create a `requirements.in` file in the module root.
