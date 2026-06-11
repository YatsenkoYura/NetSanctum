"""
Database engine, session factories, and declarative base.

Provides:
- Async engine + session for FastAPI request handlers.
- Sync engine + session for Celery workers (Celery is synchronous).
- Shared declarative Base for all module models.
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import get_settings

settings = get_settings()

# ── Async (for FastAPI) ──────────────────────────────────
async_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

# ── Sync (for Celery workers) ───────────────────────────
sync_engine = create_engine(
    settings.DATABASE_URL_SYNC,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    expire_on_commit=False,
)


# ── Shared declarative base ─────────────────────────────
class Base(DeclarativeBase):
    """All module models inherit from this base."""

    pass


# ── FastAPI dependency ───────────────────────────────────
async def get_db() -> AsyncSession:  # type: ignore[misc]
    """Yield an async database session for a single request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
