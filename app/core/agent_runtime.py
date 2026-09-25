"""Isolated agent runtime: executes cascades without touching the database.

It has no Postgres and no Redis. Everything it needs from the application it asks for
over the internal HTTP contract in app.core.agent_router, authenticated with a shared
key, and everything it needs from the outside world it fetches itself under the
egress rules in app.core.remote_fetch.
"""

import asyncio
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from app.core.agent.backend import AgentBackendUnavailableError, HttpToolBackend
from app.core.agent.engine import AgentTurnRequest, AgentTurnResult, CascadeEngine
from app.core.agent.fetch import RemoteFetchError, fetch_public_text
from app.core.agent.model import OpenAICompatibleModel
from app.core.agent.primitives import AgentFetchRequest, AgentFetchResult, AgentSpeechRequest
from app.core.agent.urls import SPEECH, TRANSCRIPTIONS, provider_endpoint

MAX_AUDIO_BYTES = 4 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm"}
MAX_TRANSCRIPT_CHARS = 500
TRANSCRIBE_TIMEOUT_SECONDS = 60
SYNTHESIZE_TIMEOUT_SECONDS = 60

RUNTIME_TOKEN = os.environ.get("AGENT_RUNTIME_TOKEN", "")
INTERNAL_KEY = os.environ.get("AGENT_INTERNAL_KEY", "")
WEB_INTERNAL_URL = os.environ.get("AGENT_WEB_URL", "http://web:8000")
CONSUMER_ID = os.environ.get("AGENT_CONSUMER_ID", "miku")
LLM_URL = os.environ.get("AGENT_LLM_URL", "")
LLM_MODEL = os.environ.get("AGENT_LLM_MODEL", "")
LLM_API_KEY = os.environ.get("AGENT_LLM_API_KEY", "")
# Local servers can think out loud before answering, which on a small model spends the
# whole token budget and returns nothing. Off by default only where it is set explicitly.
LLM_THINKING = os.environ.get("AGENT_LLM_THINKING", "1") not in {"0", "false", "False"}
STT_URL = os.environ.get("AGENT_STT_URL", "")
STT_MODEL = os.environ.get("AGENT_STT_MODEL", "whisper-1")
STT_API_KEY = os.environ.get("AGENT_STT_API_KEY", "")
TTS_URL = os.environ.get("AGENT_TTS_URL", "")
TTS_MODEL = os.environ.get("AGENT_TTS_MODEL", "tts-1")
TTS_API_KEY = os.environ.get("AGENT_TTS_API_KEY", "")
FETCH_TIMEOUT_SECONDS = 35
TOOLS_TIMEOUT_SECONDS = 15
logger = logging.getLogger(__name__)

app = FastAPI(
    title="NetSanctum agent runtime",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def verify_runtime_token(token: str) -> None:
    """Only this process's partner in web may drive the agent."""
    if not RUNTIME_TOKEN or not hmac.compare_digest(token, RUNTIME_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid agent runtime token")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "egress": True,
        "internal_configured": bool(INTERNAL_KEY),
        "providers": {
            "llm": bool(LLM_URL),
            "stt": bool(STT_URL),
            "tts": bool(TTS_URL),
        },
    }


@app.post("/v1/fetch", response_model=AgentFetchResult)
async def fetch_page(
    body: AgentFetchRequest,
    x_agent_runtime_token: str = Header(default=""),
):
    """Read one public page. Unsafe or unavailable targets fail, they never leak."""
    verify_runtime_token(x_agent_runtime_token)
    try:
        return await fetch_public_text(body.url, max_chars=body.max_chars)
    except RemoteFetchError as exc:
        raise HTTPException(status_code=422, detail="The URL could not be read") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="The URL took too long to answer") from exc
    except Exception as exc:
        logger.warning("agent fetch failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="The URL could not be read") from exc


@app.get("/v1/tools")
async def tools(x_agent_runtime_token: str = Header(default="")):
    """Proxy the application's tool catalog so the sidecar stays stateless about modules."""
    verify_runtime_token(x_agent_runtime_token)
    try:
        async with httpx.AsyncClient(timeout=TOOLS_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{WEB_INTERNAL_URL}/internal/agent/catalog",
                headers={"X-Agent-Key": INTERNAL_KEY},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="The tool catalog is unavailable") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="The tool catalog is unavailable")
    return response.json()


def _provider_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


async def _bounded_request_body(request: Request, limit: int) -> bytes:
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > limit:
            raise HTTPException(status_code=413, detail="Request body is too large")
    return bytes(content)


async def _bounded_response_body(response: httpx.Response, limit: int) -> bytes:
    length = response.headers.get("content-length")
    if length:
        try:
            if int(length) > limit:
                raise HTTPException(status_code=502, detail="Provider response is too large")
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="Provider returned an invalid content length"
            ) from exc
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > limit:
            raise HTTPException(status_code=502, detail="Provider response is too large")
    return bytes(content)


@app.post("/v1/transcribe")
async def transcribe(
    request: Request,
    x_agent_runtime_token: str = Header(default=""),
    x_agent_provider_url: str | None = Header(default=None),
    x_agent_provider_model: str | None = Header(default=None),
    x_agent_provider_key: str | None = Header(default=None),
    x_agent_provider_mode: str | None = Header(default=None),
):
    """Speech to text through the configured provider, bounded in type and size."""
    verify_runtime_token(x_agent_runtime_token)
    content_type = request.headers.get("content-type", "").partition(";")[0].lower()
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    url, model, key = _local_or_saved(
        (x_agent_provider_mode or "api"),
        x_agent_provider_url or "",
        x_agent_provider_model or "",
        x_agent_provider_key or "",
        STT_URL,
        STT_MODEL,
        STT_API_KEY,
    )
    if not url:
        raise HTTPException(status_code=503, detail="STT provider is not configured")
    audio = await _bounded_request_body(request, MAX_AUDIO_BYTES)
    if not audio:
        raise HTTPException(status_code=413, detail="Audio utterance is empty")
    try:
        async with httpx.AsyncClient(timeout=TRANSCRIBE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                provider_endpoint(url, TRANSCRIPTIONS),
                headers=_provider_headers(key),
                files={"file": ("utterance", audio, content_type)},
                data={"model": model},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="STT provider failed") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="STT provider failed")
    try:
        text = str(response.json().get("text", "")).strip()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="STT provider returned invalid text") from exc
    if not text or len(text) > MAX_TRANSCRIPT_CHARS:
        raise HTTPException(status_code=502, detail="STT provider returned invalid text")
    return {"text": text}


@app.post("/v1/synthesize")
async def synthesize(
    body: AgentSpeechRequest,
    x_agent_runtime_token: str = Header(default=""),
    x_agent_provider_url: str | None = Header(default=None),
    x_agent_provider_model: str | None = Header(default=None),
    x_agent_provider_key: str | None = Header(default=None),
    x_agent_provider_mode: str | None = Header(default=None),
):
    """Text to speech through the configured provider, bounded in type and size."""
    verify_runtime_token(x_agent_runtime_token)
    url, model, key = _local_or_saved(
        (x_agent_provider_mode or "api"),
        x_agent_provider_url or "",
        x_agent_provider_model or "",
        x_agent_provider_key or "",
        TTS_URL,
        TTS_MODEL,
        TTS_API_KEY,
    )
    if not url:
        raise HTTPException(status_code=503, detail="TTS provider is not configured")
    try:
        async with httpx.AsyncClient(timeout=SYNTHESIZE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                provider_endpoint(url, SPEECH),
                headers=_provider_headers(key),
                json={
                    "model": model,
                    "input": body.text,
                    "voice": body.voice,
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="TTS provider failed") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="TTS provider failed")
    audio = await _bounded_response_body(response, MAX_AUDIO_BYTES)
    media_type = response.headers.get("content-type", "audio/mpeg")
    if not audio or not media_type.lower().startswith("audio/"):
        raise HTTPException(status_code=502, detail="TTS provider returned invalid audio")
    return Response(audio, media_type=media_type)


__all__ = ["app", "fetch_page", "run_turn", "synthesize", "tools", "transcribe", "verify_runtime_token"]


def _local_or_saved(
    mode: str,
    saved_url: str,
    saved_model: str,
    saved_key: str,
    env_url: str,
    env_model: str,
    env_key: str,
) -> tuple[str, str, str]:
    """In local mode the sidecar serves the model it hosts, not a saved remote URL."""
    if mode == "local":
        # A hosted model service is preferred; a saved URL is the fallback when there is none.
        return (
            (env_url or saved_url).strip(),
            (env_model or saved_model or "local").strip(),
            (env_key or "").strip(),
        )
    return saved_url.strip(), (saved_model or env_model or "local").strip(), saved_key.strip()


def build_engine(request: AgentTurnRequest) -> CascadeEngine:
    """Assemble the cascade: the caller's model plus the application's contract."""
    provider = request.llm
    url, model, api_key = _local_or_saved(
        # No profile at all means "use the model this runtime hosts".
        provider.mode if provider else "local",
        provider.url if provider else "",
        provider.model if provider else "",
        provider.api_key if provider else "",
        LLM_URL,
        LLM_MODEL,
        LLM_API_KEY,
    )
    if not url:
        raise AgentBackendUnavailableError("no model configured")
    return CascadeEngine(
        model=OpenAICompatibleModel(url, model, api_key=api_key, thinking=LLM_THINKING),
        backend=HttpToolBackend(WEB_INTERNAL_URL, INTERNAL_KEY, consumer_id=CONSUMER_ID),
    )


@app.post("/v1/turn")
async def run_turn(
    body: AgentTurnRequest,
    x_agent_runtime_token: str = Header(default=""),
) -> StreamingResponse:
    """Execute one cascade, streaming step progress before the final result."""
    verify_runtime_token(x_agent_runtime_token)
    try:
        engine = build_engine(body)
    except AgentBackendUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    async def stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def publish(phase: str, data: dict) -> None:
            await queue.put(json.dumps({"type": phase, **data}, ensure_ascii=False) + "\n")

        async def produce() -> None:
            try:
                result: AgentTurnResult = await engine.run(body, on_progress=publish)
                await queue.put(
                    json.dumps({"type": "done", "result": result.model_dump(mode="json")}, ensure_ascii=False)
                    + "\n"
                )
            except Exception as exc:
                # Bounded, and never the provider payload: just enough to debug a turn.
                logger.warning("cascade failed: %s: %s", type(exc).__name__, str(exc)[:200])
                await queue.put(
                    json.dumps({"type": "failed", "reason": type(exc).__name__}, ensure_ascii=False) + "\n"
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            await task

    return StreamingResponse(stream(), media_type="application/x-ndjson")
