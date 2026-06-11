"""
NetSanctum — Single-User Self-Hosted Modular Monolith Entry Point.

Dynamically discovers and mounts all module routers.
Ensures physical filesystem access token exists at startup.
Seeds default settings.
"""

import importlib
import logging
import pkgutil
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, Base, async_engine
from app.core.security import OwnerUser
from app.core.templates import templates

settings = get_settings()
logger = logging.getLogger(__name__)

TOKEN_FILE = Path("/app/access_token.hash")


ACTIVE_MODULES = []


def _discover_modules_and_routers() -> list:
    """
    Scan app/modules/*, import packages, extract their metadata,
    and collect their APIRouter instances. Bypasses modules with missing
    dependencies or errors.
    """
    global ACTIVE_MODULES
    routers = []
    ACTIVE_MODULES = []

    import app.modules as modules_pkg

    for _importer, module_name, is_pkg in pkgutil.iter_modules(modules_pkg.__path__, prefix="app.modules."):
        if not is_pkg:
            continue

        # 1. Try to load the module package to inspect metadata
        try:
            pkg = importlib.import_module(module_name)

            # Read metadata variables from __init__.py
            title_en = getattr(pkg, "TITLE_EN", None)
            title_ru = getattr(pkg, "TITLE_RU", None)
            dashboard_url = getattr(pkg, "DASHBOARD_URL", None)

            if dashboard_url:
                ACTIVE_MODULES.append(
                    {
                        "name": module_name.split(".")[-1],
                        "title_en": title_en or module_name.split(".")[-1].capitalize(),
                        "title_ru": title_ru or module_name.split(".")[-1].capitalize(),
                        "dashboard_url": dashboard_url,
                        "order": getattr(pkg, "ORDER", 100),
                    }
                )
        except Exception as e:
            logger.error(
                "Failed to load module package %s (missing dependencies or syntax error): %s", module_name, e
            )
            continue

        # 2. Try to load the router for registered modules
        router_module_path = f"{module_name}.router"
        try:
            mod = importlib.import_module(router_module_path)
            if hasattr(mod, "router"):
                routers.append(mod.router)
                logger.info("Mounted module router: %s", module_name)
        except Exception as e:
            logger.warning("Module %s has no router.py or failed to import router: %s", module_name, e)

    # Sort active modules by their order attribute
    ACTIVE_MODULES.sort(key=lambda x: x["order"])
    return routers


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: ensure token exists, create tables. Shutdown: dispose engine."""

    # 1. Physical Access Token Generation
    if not TOKEN_FILE.is_file():
        import hashlib

        # Generate dynamic secure key
        token = secrets.token_urlsafe(32)
        try:
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            TOKEN_FILE.write_text(token_hash)

            # Write plain text to a file so it's not lost in noisy docker logs
            plain_token_file = Path("/app/access_token.txt")
            plain_token_file.write_text(
                f"YOUR MASTER TOKEN:\n\n{token}\n\n"
                f"SAVE THIS AND DELETE THIS FILE (access_token.txt) IMMEDIATELY."
            )

            # Print prominent Neo-brutalist alert to stdout for easy user discovery in logs
            print("\n" + "=" * 60)
            print("  [!] NETSANCTUM INITIALIZATION SUCCESSFUL")
            print("  [!] ACCESS TOKEN HAS BEEN GENERATED.")
            print("  [!] IT HAS BEEN SAVED TO access_token.txt IN YOUR FOLDER.")
            print("  [!] SAVE IT AND DELETE access_token.txt IMMEDIATELY.")
            print(f"      >>>  {token}  <<<")
            print("=" * 60 + "\n")
        except Exception as e:
            logger.error(f"Failed to generate physical token file: {e}")
    else:
        print("\n" + "=" * 60)
        print("  [!] NETSANCTUM ONLINE")
        print("  [!] ACCESS TOKEN LOADED FROM HASH FILE.")
        print("=" * 60 + "\n")

    # 2. Schema creation
    # 2. Dynamic Schema Discovery & Creation
    import importlib
    import pkgutil

    import app.modules as modules_pkg

    for _importer, module_name, is_pkg in pkgutil.iter_modules(modules_pkg.__path__, prefix="app.modules."):
        if is_pkg:
            try:
                importlib.import_module(f"{module_name}.models")
                logger.info(f"Loaded models for {module_name}")
            except Exception as e:
                logger.debug(f"Bypassed model discovery for {module_name}: {e}")

    from sqlalchemy import select, text

    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        try:
            await conn.execute(text("ALTER TABLE archived_videos ADD COLUMN IF NOT EXISTS subtitles JSONB;"))
            logger.info("Database auto-migration: verified subtitles column exists")
        except Exception as e:
            logger.debug(f"Subtitles column migration bypassed: {e}")
    logger.info("Database schemas verified")

    # Purge pending Celery tasks (generic cleanup)
    try:
        from app.core.scheduler import celery_app

        purged = celery_app.control.purge()
        logger.info(f"Purged {purged} pending tasks from Celery.")

        # Clear stale active download keys from Redis
        import redis

        from app.core.config import get_settings

        settings = get_settings()
        r = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

        # Scan and delete active task trackers
        for key in r.scan_iter("video_dl:*"):
            r.delete(key)
        for key in r.scan_iter("music_dl:*"):
            r.delete(key)
        logger.info("Cleared active download trackers from Redis.")
    except Exception as e:
        logger.warning(f"Could not purge Celery or Redis trackers on startup: {e}")

    # 3. Seed default Settings if empty
    try:
        from app.modules.settings.models import Setting

        async with AsyncSessionLocal() as session:
            setting_check = await session.execute(select(Setting).limit(1))
            if not setting_check.scalar_one_or_none():
                logger.info("Seeding system settings configuration...")
                default_settings = [
                    Setting(
                        scope="global",
                        key="system_theme",
                        value="neo-brutalist-dark",
                        description="Visual layout paradigm",
                        value_type="string",
                        is_secret=False,
                    ),
                    Setting(
                        scope="global",
                        key="system_language",
                        value="en",
                        description="Default application language interface",
                        value_type="string",
                        is_secret=False,
                    ),
                    Setting(
                        scope="global",
                        key="openai_api_key",
                        value="",
                        description="OpenAI / Gemini API Key",
                        value_type="string",
                        is_secret=True,
                    ),
                    Setting(
                        scope="global",
                        key="openai_base_url",
                        value="https://generativelanguage.googleapis.com/v1beta/openai/",
                        description="OpenAI-compatible Base URL",
                        value_type="string",
                        is_secret=False,
                    ),
                    Setting(
                        scope="global",
                        key="max_upload_size_mb",
                        value="5000",
                        description="Maximum raw upload limits in Megabytes",
                        value_type="integer",
                        is_secret=False,
                    ),
                    Setting(
                        scope="global",
                        key="encryption_cipher",
                        value="AES-256-GCM",
                        description="Secure filesystem block encryption protocol",
                        value_type="string",
                        is_secret=False,
                    ),
                    Setting(
                        scope="global",
                        key="external_sync_key",
                        value="sk-netsanctum-prod-998877",
                        description="Symmetric replication key for remote vaults",
                        value_type="string",
                        is_secret=True,
                    ),
                ]
                session.add_all(default_settings)
                await session.commit()
    except ImportError:
        logger.info("Settings module not installed; skipping default settings seed.")

    yield

    await async_engine.dispose()
    logger.info("Database engine disposed")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Single-User Self-Hosted Modular Monolith Backend.",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static Files ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory="/app/static"), name="static")

# ── Auto-mount module routers and register templates variables ────────────
for module_router in _discover_modules_and_routers():
    app.include_router(module_router)

templates.env.globals["active_modules"] = ACTIVE_MODULES


# ── Helper: resolve user from cookie ─────────────────────
async def _get_user_from_cookie(request: Request):
    """Verify session cookie via Redis and return static OwnerUser representation."""
    session_id = request.cookies.get("access_token")
    if not session_id:
        return None
    from app.core.security import redis_client

    if redis_client.get(f"session:{session_id}") == "1":
        return OwnerUser()
    return None


# ── Helper: resolve language preference ───────────────────
async def _get_lang(request: Request) -> str:
    """Resolve active language cookie or fall back to DB config."""
    lang = request.cookies.get("lang")
    if lang:
        return lang
    try:
        from sqlalchemy import select

        from app.modules.settings.models import Setting

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Setting).where(Setting.key == "system_language"))
            setting = result.scalar_one_or_none()
            if setting and setting.value:
                return setting.value
    except Exception:
        pass
    return "en"


# ── Root & Dashboard Routes ──────────────────────────────
@app.get("/", include_in_schema=False)
async def root(request: Request):
    """Redirect root access depending on session validity."""
    user = await _get_user_from_cookie(request)
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return RedirectResponse(url="/auth/login-page", status_code=302)


@app.get("/dashboard", include_in_schema=False)
async def dashboard(request: Request):
    """Serve the primary control room dashboard."""
    user = await _get_user_from_cookie(request)
    if not user:
        return RedirectResponse(url="/auth/login-page", status_code=302)
    lang = await _get_lang(request)
    return templates.TemplateResponse(request, "dashboard.html", {"user": user, "lang": lang})


@app.get("/set-language", include_in_schema=False)
async def set_language(request: Request, lang: str = "en"):
    """Set the lang cookie and redirect back to the referrer or home."""
    referrer = request.headers.get("referer") or "/dashboard"
    response = RedirectResponse(url=referrer, status_code=302)
    response.set_cookie(
        key="lang",
        value=lang,
        httponly=False,
        samesite="lax",
        max_age=31536000,  # 1 year
    )
    return response


@app.get("/health", tags=["System"])
async def health():
    """System health check endpoint."""
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}
