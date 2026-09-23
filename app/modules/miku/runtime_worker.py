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


async def _decide_with_llm(request: MikuDecisionRequest) -> MikuDecision:
    system = (
        "Select exactly one available function that best satisfies the request. Existing module APIs are "
        "authoritative. Prefer a catalog limit of 1 when one or any item is requested. Never invent IDs or "
        "call unavailable functions. Mutation functions only create a preview and still require user confirmation. "
        "Use miku_respond only for conversation or when no module API safely matches."
    )
    tool_names: dict[str, str] = {}
    api_tools = []
    for index, tool in enumerate(request.tools):
        name = f"api_{index}_{tool.integration_id.replace('.', '_').replace('-', '_')}"
        tool_names[name] = tool.integration_id
        api_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": f"{tool.description}. API ID: {tool.integration_id}",
                    "parameters": tool.input_schema,
                },
            }
        )
    reference_values = [item.ref for item in request.context]
    builtins = {
        "miku_help": "help",
        "miku_sources": "sources",
        "miku_repeat": "repeat",
        "miku_use_result": "reference",
        "miku_respond": "respond",
    }
    api_tools.extend(
        [
            {
                "type": "function",
                "function": {
                    "name": "miku_respond",
                    "description": "Return a short conversational response when no module API is needed or allowed.",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string", "maxLength": 500}},
                        "required": ["message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "miku_help",
                    "description": "Explain the available assistant capabilities.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "miku_sources",
                    "description": "List modules and APIs available to the assistant.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "miku_repeat",
                    "description": "Repeat the current result set without calling another API.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "miku_use_result",
                    "description": "Open or play one result from current_results.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ref": {"type": "string", "enum": reference_values or ["result:1"]},
                            "action": {"type": "string", "enum": ["open", "play"]},
                        },
                        "required": ["ref", "action"],
                    },
                },
            },
        ]
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
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "current_results": [item.model_dump(mode="json") for item in request.context],
                                "request": request.message,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ],
                "temperature": 0,
                "max_tokens": 100,
                "tools": api_tools,
                "tool_choice": "required",
            },
        ) as response,
    ):
        response.raise_for_status()
        payload = json.loads(await _bounded_response_body(response, 64 * 1024))
    calls = payload["choices"][0]["message"]["tool_calls"]
    if len(calls) != 1:
        raise ValueError("Exactly one tool call is required")
    function = calls[0]["function"]
    name = function["name"]
    raw_arguments = function.get("arguments") or {}
    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    if name in tool_names:
        return MikuDecision(
            command="invoke",
            integration_id=tool_names[name],
            parameters=arguments,
        )
    builtin = builtins.get(name)
    if builtin == "reference":
        return MikuDecision.model_validate({"command": arguments["action"], "argument": arguments["ref"]})
    if builtin == "respond":
        return MikuDecision(command="respond", argument=str(arguments["message"])[:500])
    if builtin:
        return MikuDecision.model_validate({"command": builtin})
    raise ValueError("Unknown tool call")


@app.post("/v1/decide", response_model=MikuDecision)
async def decide(
    body: MikuDecisionRequest,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
):
    verify_runtime_token(runtime_token)
    if LLM_URL:
        try:
            return await _decide_with_llm(body)
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
