"""Web-side client for the isolated agent runtime.

The web process keeps ownership of the session, the transcript and the user-facing
reply; the sidecar only decides what to do next. If the sidecar is missing or breaks,
callers fall back to the in-process assistant instead of losing the turn.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.agent.engine import (
    AgentHistoryTurn,
    AgentProviderProfile,
    AgentTurnRequest,
    AgentTurnResult,
)
from app.core.agent.primitives import AgentSpeechRequest
from app.core.agent.references import AgentReference

TURN_TIMEOUT_SECONDS = 420.0
VOICE_TIMEOUT_SECONDS = 60.0
MAX_TRANSCRIPT_CHARS = 500
MAX_AUDIO_BYTES = 4 * 1024 * 1024
PROVIDER_MODE_HEADER = "X-Agent-Provider-Mode"


class AgentAudio:
    """Synthesised speech, kept in memory and bounded like everything else."""

    __slots__ = ("content", "media_type")

    def __init__(self, content: bytes, media_type: str) -> None:
        self.content = content
        self.media_type = media_type


class AgentRuntimeUnavailableError(RuntimeError):
    """The isolated runtime could not complete the turn."""


class AgentTurnProgress:
    """One streamed progress frame, already reduced to what the UI needs."""

    __slots__ = ("payload", "type")

    def __init__(self, type: str, payload: dict[str, Any]) -> None:
        self.type = type
        self.payload = payload


class AgentClient:
    def __init__(
        self,
        enabled: bool,
        url: str,
        token: str,
        *,
        timeout: float = TURN_TIMEOUT_SECONDS,
        transport: Any | None = None,
    ) -> None:
        self.enabled = enabled
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport

    def _headers(self, provider: Any = None) -> dict[str, str]:
        return {
            "X-Agent-Runtime-Token": self.token,
            "Content-Type": "application/json",
            **_provider_headers(provider),
        }

    async def transcribe(self, audio: bytes, content_type: str, provider: Any = None) -> str:
        """Speech to text. Client-side recognition never reaches the server."""
        if not self.enabled:
            raise AgentRuntimeUnavailableError("Speech recognition is not configured")
        if getattr(provider, "mode", "api") == "client":
            raise AgentRuntimeUnavailableError("Speech recognition runs on the client")
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise AgentRuntimeUnavailableError("The audio utterance is empty or too large")
        try:
            async with httpx.AsyncClient(timeout=VOICE_TIMEOUT_SECONDS, transport=self.transport) as client:
                response = await client.post(
                    f"{self.url}/v1/transcribe",
                    headers={**self._headers(provider), "Content-Type": content_type},
                    content=audio,
                )
        except httpx.HTTPError as exc:
            raise AgentRuntimeUnavailableError("Speech recognition is unavailable") from exc
        if response.status_code >= 400:
            raise AgentRuntimeUnavailableError("Speech recognition is unavailable")
        text = str(response.json().get("text", "")).strip()
        if not text or len(text) > MAX_TRANSCRIPT_CHARS:
            raise AgentRuntimeUnavailableError("Speech recognition returned invalid text")
        return text

    async def synthesize(self, text: str, voice: str = "alloy", provider: Any = None) -> AgentAudio:
        """Text to speech. Client-side synthesis never reaches the server."""
        if not self.enabled:
            raise AgentRuntimeUnavailableError("Speech synthesis is not configured")
        if getattr(provider, "mode", "api") == "client":
            raise AgentRuntimeUnavailableError("Speech synthesis runs on the client")
        body = AgentSpeechRequest(text=text, voice=voice)
        try:
            async with httpx.AsyncClient(timeout=VOICE_TIMEOUT_SECONDS, transport=self.transport) as client:
                response = await client.post(
                    f"{self.url}/v1/synthesize",
                    headers=self._headers(provider),
                    json=body.model_dump(mode="json"),
                )
        except httpx.HTTPError as exc:
            raise AgentRuntimeUnavailableError("Speech synthesis is unavailable") from exc
        if response.status_code >= 400 or not response.content:
            raise AgentRuntimeUnavailableError("Speech synthesis is unavailable")
        if len(response.content) > MAX_AUDIO_BYTES:
            raise AgentRuntimeUnavailableError("Speech synthesis returned too much audio")
        return AgentAudio(response.content, response.headers.get("content-type", "audio/mpeg"))

    async def turn(
        self,
        *,
        message: str,
        session_id: str = "",
        history: list[AgentHistoryTurn] | None = None,
        references: list[AgentReference] | None = None,
        llm: AgentProviderProfile | None = None,
    ) -> AsyncIterator[AgentTurnProgress | AgentTurnResult]:
        """Run one cascade, yielding progress frames and finally the result."""
        body = AgentTurnRequest(
            message=message,
            session_id=session_id,
            history=history or [],
            references=references or [],
            llm=llm,
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST",
                    f"{self.url}/v1/turn",
                    headers=self._headers(),
                    json=body.model_dump(mode="json"),
                ) as response:
                    if response.status_code != 200:
                        raise AgentRuntimeUnavailableError(f"agent runtime returned {response.status_code}")
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        frame = _parse_frame(line)
                        if frame is None:
                            continue
                        if frame.get("type") == "done":
                            yield AgentTurnResult.model_validate(frame.get("result") or {})
                            return
                        if frame.get("type") == "failed":
                            raise AgentRuntimeUnavailableError("the cascade failed")
                        yield AgentTurnProgress(str(frame.get("type") or ""), frame)
        except httpx.HTTPError as exc:
            raise AgentRuntimeUnavailableError("the agent runtime is unreachable") from exc


def _provider_headers(provider: Any) -> dict[str, str]:
    """Per-call provider credentials: the sidecar keeps no secrets at rest."""
    if provider is None:
        return {}
    headers = {}
    if getattr(provider, "url", ""):
        headers["X-Agent-Provider-Url"] = str(provider.url)
    if getattr(provider, "model", ""):
        headers["X-Agent-Provider-Model"] = str(provider.model)
    if getattr(provider, "api_key", ""):
        headers["X-Agent-Provider-Key"] = str(provider.api_key)
    if getattr(provider, "mode", ""):
        headers[PROVIDER_MODE_HEADER] = str(provider.mode)
    return headers


def _parse_frame(line: str) -> dict[str, Any] | None:
    try:
        frame = json.loads(line)
    except ValueError:
        return None
    return frame if isinstance(frame, dict) else None


__all__ = [
    "TURN_TIMEOUT_SECONDS",
    "AgentClient",
    "AgentRuntimeUnavailableError",
    "AgentTurnProgress",
]
