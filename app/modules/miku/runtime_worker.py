import hmac
import json
import logging
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response

from app.modules.miku.planner import MikuQueryError, plan_with_rules
from app.modules.miku.schemas import MikuDecision, MikuDecisionRequest, MikuSpeechRequest

RUNTIME_TOKEN = os.getenv("MIKU_RUNTIME_TOKEN", "")
LLM_URL = os.getenv("MIKU_LLM_URL", "").strip()
LLM_MODEL = os.getenv("MIKU_LLM_MODEL", "qwen2.5:7b").strip()
STT_URL = os.getenv("MIKU_STT_URL", "").strip()
STT_MODEL = os.getenv("MIKU_STT_MODEL", "whisper-1").strip()
TTS_URL = os.getenv("MIKU_TTS_URL", "").strip()
TTS_MODEL = os.getenv("MIKU_TTS_MODEL", "tts-1").strip()
MAX_AUDIO_BYTES = 4 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm"}
logger = logging.getLogger(__name__)


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


app = FastAPI(
    title="MIKU Runtime",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def verify_runtime_token(token: str) -> None:
    if not RUNTIME_TOKEN or not hmac.compare_digest(token, RUNTIME_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid runtime token")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "mode": "llm" if LLM_URL else "rules",
        "protocol_version": 2,
        "providers": {"llm": bool(LLM_URL), "stt": bool(STT_URL), "tts": bool(TTS_URL)},
    }


async def _decide_with_llm(message: str) -> MikuDecision:
    system = (
        "Return one JSON object with keys command and argument. Allowed commands: help, sources, list, "
        "find, repeat, discover, open, play, archive, note, bookmark. Use find for local library search, "
        "discover for YouTube search, result:N for open/play/archive, note for short Vault notes, and "
        "bookmark for one HTTP URL. Never invent other commands."
    )
    async with (
        httpx.AsyncClient(timeout=30) as client,
        client.stream(
            "POST",
            LLM_URL,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
                "temperature": 0,
                "max_tokens": 100,
                "response_format": {"type": "json_object"},
            },
        ) as response,
    ):
        response.raise_for_status()
        payload = json.loads(await _bounded_response_body(response, 64 * 1024))
    content = payload["choices"][0]["message"]["content"]
    return MikuDecision.model_validate(json.loads(content))


@app.post("/v1/decide", response_model=MikuDecision)
async def decide(
    body: MikuDecisionRequest,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
):
    verify_runtime_token(runtime_token)
    if LLM_URL:
        try:
            return await _decide_with_llm(body.message)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("MIKU LLM provider failed validation; using rule planner")
    try:
        return plan_with_rules(body.message)
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/transcribe")
async def transcribe(
    request: Request,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
):
    verify_runtime_token(runtime_token)
    content_type = request.headers.get("content-type", "").partition(";")[0].lower()
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    if not STT_URL:
        raise HTTPException(status_code=503, detail="STT provider is not configured")
    audio = await _bounded_request_body(request, MAX_AUDIO_BYTES)
    if not audio:
        raise HTTPException(status_code=413, detail="Audio utterance is empty")
    try:
        async with (
            httpx.AsyncClient(timeout=60) as client,
            client.stream(
                "POST",
                STT_URL,
                files={"file": ("utterance", audio, content_type)},
                data={"model": STT_MODEL},
            ) as response,
        ):
            response.raise_for_status()
            payload = json.loads(await _bounded_response_body(response, 64 * 1024))
        text = str(payload.get("text", "")).strip()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="STT provider failed") from exc
    if not text or len(text) > 500:
        raise HTTPException(status_code=502, detail="STT provider returned invalid text")
    return {"text": text}


@app.post("/v1/synthesize")
async def synthesize(
    body: MikuSpeechRequest,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
):
    verify_runtime_token(runtime_token)
    if not TTS_URL:
        raise HTTPException(status_code=503, detail="TTS provider is not configured")
    try:
        async with (
            httpx.AsyncClient(timeout=60) as client,
            client.stream(
                "POST",
                TTS_URL,
                json={"model": TTS_MODEL, "input": body.text, "voice": body.voice},
            ) as response,
        ):
            response.raise_for_status()
            media_type = response.headers.get("content-type", "audio/mpeg")
            audio = await _bounded_response_body(response, MAX_AUDIO_BYTES)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="TTS provider failed") from exc
    if not audio or not media_type.lower().startswith("audio/"):
        raise HTTPException(status_code=502, detail="TTS provider returned invalid audio")
    return Response(audio, media_type=media_type)
