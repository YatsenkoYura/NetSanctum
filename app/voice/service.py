"""HTTP surface of the voice service, shaped like the speech APIs it replaces.

The agent runtime already posts to `/v1/audio/transcriptions` and
`/v1/audio/speech` in the OpenAI form, because those are the paths a configured
provider is expected to answer on. Exposing exactly those here means pointing the
assistant's speech settings at this service is a change of URL and nothing else,
and a hosted provider stays a drop-in alternative rather than a different code
path.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from app.voice import VoiceService, build
from app.voice.stt import TranscriptionError
from app.voice.tts import MAX_TEXT_CHARS, SUPPORTED_LANGUAGES, SynthesisError

# The service's own timings are the only way to see where a slow reply went, and
# uvicorn configures logging for itself alone, so this has to be set up explicitly.
logging.basicConfig(
    level=os.environ.get("VOICE_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
_BACKGROUND: set[asyncio.Task] = set()

# How long to wait for the first engine before serving anyway. Loading it is worth
# seconds of the first reply; refusing to serve because of it is not.
WARM_TIMEOUT_SECONDS = 120.0
SYNTHESIS_WARNING_SECONDS = 2.0

_service = build()


async def _warm(service: VoiceService) -> None:
    """Load the default engine before the first request asks for it.

    Cold, the first Russian reply costs eight seconds and the first English one
    twenty-five, all of it spent loading a model. Paying that during startup moves
    the cost off the first thing anybody says. The other language follows in the
    background, because it is the rarer one and must not delay the common case.
    """
    primary = service.settings.default_lang
    started = time.perf_counter()
    try:
        await asyncio.wait_for(service.synthesiser.warm(primary), timeout=WARM_TIMEOUT_SECONDS)
        logger.info("voice engine %s ready in %.1f s", primary, time.perf_counter() - started)
    except Exception:
        logger.exception("could not warm the %s engine; it will load on first use", primary)
    # Only pre-load the other language when there is room for it. Warming it into a
    # full slot would evict the language that was just warmed, so the first reply in
    # the common language would pay the load the warm-up was meant to avoid.
    for language in SUPPORTED_LANGUAGES:
        if language == primary or len(service.residency.resident()) >= service.settings.max_resident:
            continue
        # Held in a module set so the task is not collected mid-flight, and so a
        # shutdown has something to wait on rather than an orphan.
        _BACKGROUND.add(asyncio.create_task(_warm_quietly(service, language)))


async def _warm_quietly(service: VoiceService, language: str) -> None:
    try:
        started = time.perf_counter()
        await service.synthesiser.warm(language)
        logger.info("voice engine %s ready in %.1f s", language, time.perf_counter() - started)
    except Exception:
        # A language that cannot be preloaded is not a reason to refuse requests.
        logger.warning("could not pre-load the %s engine", language, exc_info=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await _warm(_service)
    try:
        yield
    finally:
        for task in list(_BACKGROUND):
            task.cancel()


app = FastAPI(title="NetSanctum voice", docs_url=None, redoc_url=None, lifespan=lifespan)


class SpeechRequest(BaseModel):
    model: str | None = None
    input: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    voice: str | None = Field(default=None, max_length=64)
    response_format: str = Field(default="wav", max_length=16)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    language: str | None = Field(default=None, max_length=8)


class TranscriptResponse(BaseModel):
    text: str


@app.get("/health")
async def health() -> dict:
    """What is loaded right now, which is also how the memory budget is checked."""
    return _service.health()


@app.post("/v1/audio/transcriptions", response_model=TranscriptResponse)
async def transcribe(
    file: UploadFile = File(...),
    model: str | None = Form(default=None),
    language: str | None = Form(default=None),
    response_format: str = Form(default="json"),
) -> TranscriptResponse:
    audio = await file.read()
    try:
        text = await _service.transcriber.transcribe(
            audio,
            filename=file.filename or "utterance.webm",
            content_type=file.content_type or "audio/webm",
            language=_clean_language(language),
        )
    except TranscriptionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return TranscriptResponse(text=text)


@app.post("/v1/audio/speech")
async def speech(body: SpeechRequest) -> Response:
    if body.response_format not in {"wav", "pcm"}:
        raise HTTPException(status_code=400, detail="only wav is produced")
    try:
        audio, media_type = await _service.synthesiser.synthesise(
            body.input,
            language=_clean_language(body.language),
            speaker=body.voice,
        )
    except SynthesisError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(
        content=audio,
        media_type=media_type,
        headers={"Cache-Control": "no-store", "X-Voice-Language": _spoken_language(body)},
    )


@app.get("/v1/audio/voices")
async def voices(language: str | None = None) -> dict:
    """What can be asked for, so a caller can offer a choice instead of guessing."""
    lang = _clean_language(language) or _service.settings.default_lang
    return {"language": lang, "voices": _service.synthesiser.speakers(lang)}


def _clean_language(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.casefold().split("-")[0].split("_")[0]
    return candidate if candidate in SUPPORTED_LANGUAGES else None


def _spoken_language(body: SpeechRequest) -> str:
    from app.voice.tts import guess_language

    return _clean_language(body.language) or guess_language(body.input, _service.settings.default_lang)


@app.on_event("startup")
async def _warn_about_a_bad_model() -> None:
    """Refuse to look healthy while recognition is silently degraded."""
    if not _service.transcriber.models_present():
        logger.warning("the configured recognition model is not on disk")
    if _service.transcriber.english_only_model():
        logger.error(
            "the configured recognition model is the English-only variant; "
            "Russian recognition will be materially worse"
        )


if os.environ.get("VOICE_RELOAD", "0") == "1":  # pragma: no cover - development aid
    logger.warning("voice service debug reload enabled")
