from dataclasses import dataclass
from time import monotonic

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.secret_values import decrypt_secret_value
from app.modules.miku.schemas import (
    MikuProviderInput,
    MikuProviderMode,
    MikuProviderSettingsResponse,
    MikuProviderSettingsUpdate,
    MikuProviderStatus,
)
from app.modules.settings.service import resolve_many, upsert_setting

PROVIDER_KINDS = ("llm", "stt", "tts")
DEFAULT_MODELS = {"llm": "gpt-4o-mini", "stt": "whisper-1", "tts": "tts-1"}
# Settings change rarely but load on every turn, transcribe and speech call.
# A short TTL keeps the dashboard save effective within a minute.
PROVIDER_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class MikuProviderConfig:
    url: str = ""
    model: str = ""
    api_key: str = ""
    mode: MikuProviderMode = "api"

    @property
    def configured(self) -> bool:
        if self.mode == "client":
            return True
        if self.mode == "local":
            return bool(self.url)
        return bool(self.url and self.model)

    @property
    def server_callable(self) -> bool:
        """Whether the NetSanctum runtime can call this provider itself."""
        return self.mode in {"api", "local"} and bool(self.url)


@dataclass(frozen=True, slots=True)
class MikuProviderBundle:
    llm: MikuProviderConfig
    stt: MikuProviderConfig
    tts: MikuProviderConfig


PROVIDER_MODES: dict[str, tuple[MikuProviderMode, ...]] = {
    "llm": ("api", "local"),
    "stt": ("api", "local", "client"),
    "tts": ("api", "local", "client"),
}

_bundle_cache: dict[int, tuple[float, MikuProviderBundle]] = {}


def _keys() -> list[str]:
    return [
        f"miku_{kind}_{field}" for kind in PROVIDER_KINDS for field in ("url", "model", "api_key", "mode")
    ]


async def load_provider_bundle(db: AsyncSession, user_id: int) -> MikuProviderBundle:
    cached = _bundle_cache.get(user_id)
    if cached and monotonic() - cached[0] < PROVIDER_CACHE_TTL_SECONDS:
        return cached[1]
    settings = await resolve_many(db, keys=_keys(), module_name="miku", user_id=user_id)

    def provider(kind: str) -> MikuProviderConfig:
        def value(field: str) -> str:
            setting = settings.get(f"miku_{kind}_{field}")
            if not setting:
                return ""
            return decrypt_secret_value(setting.value) if setting.is_secret else setting.value

        raw_mode = value("mode").strip().lower()
        allowed = PROVIDER_MODES[kind]
        mode: MikuProviderMode = raw_mode if raw_mode in allowed else "api"

        return MikuProviderConfig(
            url=value("url").strip(),
            model=value("model").strip() or DEFAULT_MODELS[kind],
            api_key=value("api_key").strip(),
            mode=mode,
        )

    bundle = MikuProviderBundle(llm=provider("llm"), stt=provider("stt"), tts=provider("tts"))
    _bundle_cache[user_id] = (monotonic(), bundle)
    return bundle


def provider_settings_response(bundle: MikuProviderBundle) -> MikuProviderSettingsResponse:
    def status(provider: MikuProviderConfig) -> MikuProviderStatus:
        return MikuProviderStatus(
            url=provider.url,
            model=provider.model,
            api_key_set=bool(provider.api_key),
            mode=provider.mode,
        )

    return MikuProviderSettingsResponse(
        llm=status(bundle.llm),
        stt=status(bundle.stt),
        tts=status(bundle.tts),
    )


async def save_provider_settings(
    db: AsyncSession,
    user_id: int,
    payload: MikuProviderSettingsUpdate,
) -> MikuProviderBundle:
    for kind in PROVIDER_KINDS:
        provider: MikuProviderInput = getattr(payload, kind)
        for field in ("url", "model", "mode"):
            await upsert_setting(
                db,
                key=f"miku_{kind}_{field}",
                value=getattr(provider, field),
                scope="user",
                user_id=user_id,
                description=f"MIKU {kind.upper()} OpenAI-compatible {field}",
            )
        if provider.api_key:
            await upsert_setting(
                db,
                key=f"miku_{kind}_api_key",
                value=provider.api_key,
                scope="user",
                user_id=user_id,
                description=f"MIKU {kind.upper()} OpenAI-compatible API key",
                is_secret=True,
            )
    await db.flush()
    _bundle_cache.pop(user_id, None)
    return await load_provider_bundle(db, user_id)


def invalidate_provider_cache(user_id: int) -> None:
    _bundle_cache.pop(user_id, None)
