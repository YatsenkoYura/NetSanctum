from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.secret_values import decrypt_secret_value
from app.modules.miku.schemas import (
    MikuProviderInput,
    MikuProviderSettingsResponse,
    MikuProviderSettingsUpdate,
    MikuProviderStatus,
)
from app.modules.settings.service import resolve_many, upsert_setting

PROVIDER_KINDS = ("llm", "stt", "tts")
DEFAULT_MODELS = {"llm": "gpt-4o-mini", "stt": "whisper-1", "tts": "tts-1"}


@dataclass(frozen=True, slots=True)
class MikuProviderConfig:
    url: str = ""
    model: str = ""
    api_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.model)


@dataclass(frozen=True, slots=True)
class MikuProviderBundle:
    llm: MikuProviderConfig
    stt: MikuProviderConfig
    tts: MikuProviderConfig


def _keys() -> list[str]:
    return [f"miku_{kind}_{field}" for kind in PROVIDER_KINDS for field in ("url", "model", "api_key")]


async def load_provider_bundle(db: AsyncSession, user_id: int) -> MikuProviderBundle:
    settings = await resolve_many(db, keys=_keys(), module_name="miku", user_id=user_id)

    def provider(kind: str) -> MikuProviderConfig:
        def value(field: str) -> str:
            setting = settings.get(f"miku_{kind}_{field}")
            if not setting:
                return ""
            return decrypt_secret_value(setting.value) if setting.is_secret else setting.value

        return MikuProviderConfig(
            url=value("url").strip(),
            model=value("model").strip() or DEFAULT_MODELS[kind],
            api_key=value("api_key").strip(),
        )

    return MikuProviderBundle(llm=provider("llm"), stt=provider("stt"), tts=provider("tts"))


def provider_settings_response(bundle: MikuProviderBundle) -> MikuProviderSettingsResponse:
    def status(provider: MikuProviderConfig) -> MikuProviderStatus:
        return MikuProviderStatus(
            url=provider.url,
            model=provider.model,
            api_key_set=bool(provider.api_key),
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
        for field in ("url", "model"):
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
    return await load_provider_bundle(db, user_id)
