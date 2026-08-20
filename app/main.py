"""
NetSanctum — Single-User Self-Hosted Modular Monolith Entry Point.

Dynamically discovers and mounts all module routers.
Ensures physical filesystem access token exists at startup.
Seeds default settings.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core.config import get_settings, validate_runtime_security
from app.core.database import AsyncSessionLocal, async_engine
from app.core.http_security import security_headers_middleware
from app.core.modules import module_registry
from app.core.observability import configure_observability, process_role
from app.core.security import OwnerUser, get_current_user
from app.core.templates import templates

settings = get_settings()
configure_observability(process_role("web"))
logger = logging.getLogger(__name__)

TOKEN_FILE = Path(settings.ACCESS_TOKEN_HASH_PATH)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup: ensure token exists, create tables. Shutdown: dispose engine."""

    validate_runtime_security(settings)

    # 1. Physical Access Token Generation
    if not TOKEN_FILE.is_file():
        import hashlib

        # Generate dynamic secure key
        token = secrets.token_urlsafe(32)
        try:
            # Write plain text to a file so it's not lost in noisy docker logs
            plain_token_file = Path(settings.ACCESS_TOKEN_PLAINTEXT_PATH)
            plain_token_file.parent.mkdir(parents=True, exist_ok=True)
            plain_token_file.write_text(
                f"YOUR MASTER TOKEN:\n\n{token}\n\n"
                f"SAVE THIS AND DELETE THIS FILE (access_token.txt) IMMEDIATELY."
            )
            plain_token_file.chmod(0o600)

            # Persist the verifier last so a partial bootstrap cannot lock out the owner.
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            TOKEN_FILE.write_text(token_hash)
            TOKEN_FILE.chmod(0o600)

            # Print prominent Neo-brutalist alert to stdout for easy user discovery in logs
            print("\n" + "=" * 60)
            print("  [!] NETSANCTUM INITIALIZATION SUCCESSFUL")
            print("  [!] ACCESS TOKEN HAS BEEN GENERATED.")
            print("  [!] IT HAS BEEN SAVED TO access_token.txt IN YOUR FOLDER.")
            print("  [!] SAVE IT AND DELETE access_token.txt IMMEDIATELY.")
            print("=" * 60 + "\n")
        except Exception as error:
            raise RuntimeError("Failed to generate owner bootstrap token") from error
    else:
        print("\n" + "=" * 60)
        print("  [!] NETSANCTUM ONLINE")
        print("  [!] ACCESS TOKEN LOADED FROM HASH FILE.")
        print("=" * 60 + "\n")

    # 2. Register active module models. Schema changes remain managed by Alembic.
    module_registry.import_models()
    logger.info("Database schemas verified (managed by Alembic)")

    # Encrypt secret settings created by older versions before serving requests.
    try:
        from app.core.secret_values import rotate_secret_value, secret_value_uses_current_key
        from app.modules.settings.models import Setting

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Setting).where(Setting.is_secret.is_(True)))
            changed = False
            for setting in result.scalars():
                if not secret_value_uses_current_key(setting.value):
                    setting.value = rotate_secret_value(setting.value)
                    changed = True
            if changed:
                await session.commit()
    except ImportError:
        logger.info("Settings module not installed; skipping secret migration.")

    # 3. Seed default Settings if empty
    try:
        from app.core.secret_values import encrypt_secret_value
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
                        value=encrypt_secret_value(""),
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
                        value=encrypt_secret_value(secrets.token_urlsafe(32)),
                        description="Symmetric replication key for remote vaults",
                        value_type="string",
                        is_secret=True,
                    ),
                ]
                session.add_all(default_settings)
                await session.commit()
    except ImportError:
        logger.info("Settings module not installed; skipping default settings seed.")

    try:
        if process_role("web") == "web":
            from app.core.encryption_migration import start_encryption_migration

            start_encryption_migration()
        await module_registry.run_startup_hooks()
        yield
    finally:
        if process_role("web") == "web":
            from app.core.encryption_migration import stop_encryption_migration

            await stop_encryption_migration()
        await module_registry.run_shutdown_hooks()
        await async_engine.dispose()
        logger.info("Database engine disposed")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Single-User Self-Hosted Modular Monolith Backend.",
    lifespan=lifespan,
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[host.strip() for host in settings.TRUSTED_HOSTS.split(",") if host.strip()],
)
app.middleware("http")(security_headers_middleware)

# ── CORS ─────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=(
        r"https://([a-z0-9-]+\.)?"
        r"(mangalib\.me|ranobelib\.me|hentailib\.org|slashlib\.me|comixlib\.me|anilib\.me|"
        r"ranobehub\.org|ranobe\.space|mangadex\.org|novel-bin\.net|novel-bin\.com)"
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static Files ─────────────────────────────────────────
static_directory = Path("/app/static")
if not static_directory.is_dir():
    static_directory = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_directory), name="static")


# ── Auto-mount module routers and register templates variables ────────────
def _module_guard(module_id: str):
    async def require_active_module():
        if not module_registry.is_active(module_id):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Module {module_id!r} is not available",
            )

    return require_active_module


for module_id, module_router in module_registry.load_routers():
    app.include_router(module_router, dependencies=[Depends(_module_guard(module_id))])

from app.core.integrations_router import router as integrations_router
from app.core.packages_router import router as packages_router

app.include_router(packages_router)
app.include_router(integrations_router)

templates.env.globals["active_modules"] = module_registry.navigation


# ── Helper: resolve user from cookie ─────────────────────
async def _get_user_from_cookie(request: Request):
    """Verify session cookie via Redis and return static OwnerUser representation."""
    session_id = request.cookies.get("access_token")
    if not session_id:
        return None
    from app.core.security import redis_client

    if await redis_client.get(f"session:{session_id}") == "1":
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


@app.post("/set-language", include_in_schema=False)
async def set_language(lang: str = Form("en"), next_url: str = Form("/dashboard")):
    """Set a supported language and redirect only to a local path."""
    if lang not in {"en", "ru"}:
        lang = "en"
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/dashboard"
    response = RedirectResponse(url=next_url, status_code=303)
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


@app.get("/api/modules", tags=["System"])
async def module_diagnostics(user=Depends(get_current_user)):
    """Return installed module versions, activation states, and load failures."""
    return module_registry.diagnostics()


@app.get("/api/encryption-migration", tags=["System"])
async def encryption_migration_diagnostics(user=Depends(get_current_user)):
    """Return progress without exposing key material or encrypted paths."""
    from app.core.encryption_migration import encryption_migration_status

    return encryption_migration_status()
