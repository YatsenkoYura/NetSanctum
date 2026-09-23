import logging
from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.modules.miku.planner import MikuQueryError, plan_with_rules
from app.modules.miku.schemas import (
    MikuDecision,
    MikuDecisionRequest,
    MikuRuntimeCapabilities,
    MikuSpeechRequest,
)

logger = logging.getLogger(__name__)


class MikuRuntimeUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MikuAudio:
    content: bytes
    media_type: str


class MikuRuntimeClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        enabled: bool | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.MIKU_RUNTIME_URL).rstrip("/")
        self.token = token if token is not None else settings.MIKU_RUNTIME_TOKEN
        self.enabled = settings.MIKU_RUNTIME_ENABLED if enabled is None else enabled
        self.transport = transport

    async def decide(self, message: str) -> MikuDecision:
        if not self.enabled:
            return plan_with_rules(message)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=10,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    "/v1/decide",
                    headers={"X-Miku-Runtime-Token": self.token},
                    json=MikuDecisionRequest(message=message).model_dump(mode="json"),
                )
        except httpx.HTTPError:
            logger.warning("MIKU runtime is unavailable; using the local rule planner")
            return plan_with_rules(message)
        if response.status_code == 422:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise MikuQueryError(detail or "Runtime rejected the query")
        if response.status_code >= 400:
            logger.warning(
                "MIKU runtime returned status %s; using the local rule planner", response.status_code
            )
            return plan_with_rules(message)
        return MikuDecision.model_validate(response.json())

    async def capabilities(self) -> MikuRuntimeCapabilities:
        if not self.enabled:
            return MikuRuntimeCapabilities(enabled=False)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=3, transport=self.transport
            ) as client:
                response = await client.get("/health")
                response.raise_for_status()
            providers = response.json().get("providers", {})
            return MikuRuntimeCapabilities(
                enabled=True,
                llm=providers.get("llm") is True,
                stt=providers.get("stt") is True,
                tts=providers.get("tts") is True,
            )
        except (httpx.HTTPError, TypeError, ValueError):
            return MikuRuntimeCapabilities(enabled=False)

    async def transcribe(self, audio: bytes, content_type: str) -> str:
        if not self.enabled:
            raise MikuRuntimeUnavailableError("Local speech recognition is not configured")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=60, transport=self.transport
            ) as client:
                response = await client.post(
                    "/v1/transcribe",
                    headers={
                        "X-Miku-Runtime-Token": self.token,
                        "Content-Type": content_type,
                    },
                    content=audio,
                )
        except httpx.HTTPError as exc:
            raise MikuRuntimeUnavailableError("Local speech recognition is unavailable") from exc
        if response.status_code >= 400:
            raise MikuRuntimeUnavailableError("Local speech recognition is unavailable")
        text = str(response.json().get("text", "")).strip()
        if not text or len(text) > 500:
            raise MikuRuntimeUnavailableError("Local speech recognition returned invalid text")
        return text

    async def synthesize(self, text: str, voice: str = "alloy") -> MikuAudio:
        if not self.enabled:
            raise MikuRuntimeUnavailableError("Local speech synthesis is not configured")
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=60, transport=self.transport
            ) as client:
                response = await client.post(
                    "/v1/synthesize",
                    headers={"X-Miku-Runtime-Token": self.token},
                    json=MikuSpeechRequest(text=text, voice=voice).model_dump(mode="json"),
                )
        except httpx.HTTPError as exc:
            raise MikuRuntimeUnavailableError("Local speech synthesis is unavailable") from exc
        if response.status_code >= 400 or not response.content:
            raise MikuRuntimeUnavailableError("Local speech synthesis is unavailable")
        return MikuAudio(response.content, response.headers.get("content-type", "audio/mpeg"))


miku_runtime_client = MikuRuntimeClient()
