import hmac
import json
import logging
import os
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response

from app.modules.miku.planner import MikuQueryError, plan_with_rules
from app.modules.miku.schemas import (
    MikuCompanionRequest,
    MikuCompanionResponse,
    MikuDecision,
    MikuDecisionRequest,
    MikuSpeechRequest,
)

RUNTIME_TOKEN = os.getenv("MIKU_RUNTIME_TOKEN", "")
LLM_URL = os.getenv("MIKU_LLM_URL", "").strip()
LLM_MODEL = os.getenv("MIKU_LLM_MODEL", "qwen2.5:7b").strip()
LLM_API_KEY = os.getenv("MIKU_LLM_API_KEY", "").strip()
STT_URL = os.getenv("MIKU_STT_URL", "").strip()
STT_MODEL = os.getenv("MIKU_STT_MODEL", "whisper-1").strip()
STT_API_KEY = os.getenv("MIKU_STT_API_KEY", "").strip()
TTS_URL = os.getenv("MIKU_TTS_URL", "").strip()
TTS_MODEL = os.getenv("MIKU_TTS_MODEL", "tts-1").strip()
TTS_API_KEY = os.getenv("MIKU_TTS_API_KEY", "").strip()
MAX_AUDIO_BYTES = 4 * 1024 * 1024
ALLOWED_AUDIO_TYPES = {"audio/mp4", "audio/mpeg", "audio/ogg", "audio/wav", "audio/webm"}
logger = logging.getLogger(__name__)
BUILTIN_FUNCTIONS = {
    "miku_help": "help",
    "miku_sources": "sources",
    "miku_repeat": "repeat",
    "miku_use_result": "reference",
}


def _provider_endpoint(url: str, endpoint: str) -> str:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if not path.endswith(endpoint):
        path = f"{path}{endpoint}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def _provider_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _truncate_log(value: str, limit: int = 8000) -> str:
    return value if len(value) <= limit else f"{value[:limit]}...<truncated>"


def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    sanitized = dict(headers)
    if "Authorization" in sanitized:
        sanitized["Authorization"] = "Bearer <redacted>"
    return sanitized


def _log_provider_request(kind: str, url: str, headers: dict[str, str], payload) -> None:
    logger.warning(
        "MIKU %s provider request url=%s headers=%s payload=%s",
        kind,
        url,
        _sanitize_headers(headers),
        _truncate_log(json.dumps(payload, ensure_ascii=False, default=str)),
    )


def _log_provider_response(kind: str, status_code: int, body: str) -> None:
    logger.warning(
        "MIKU %s provider response status=%s body=%s",
        kind,
        status_code,
        _truncate_log(body),
    )


async def _json_provider_response(kind: str, response: httpx.Response) -> dict:
    raw = (await _bounded_response_body(response, 64 * 1024)).decode("utf-8", errors="replace")
    _log_provider_response(kind, response.status_code, raw)
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Provider returned status {response.status_code}",
            request=response.request,
            response=response,
        )
    return json.loads(raw)


async def _audio_provider_response(kind: str, response: httpx.Response) -> bytes:
    audio = await _bounded_response_body(response, MAX_AUDIO_BYTES)
    _log_provider_response(
        kind,
        response.status_code,
        json.dumps(
            {
                "content_type": response.headers.get("content-type", "application/octet-stream"),
                "bytes": len(audio),
            }
        ),
    )
    if response.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"Provider returned status {response.status_code}",
            request=response.request,
            response=response,
        )
    return audio


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
    version="0.3.0",
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
        "protocol_version": 3,
        "providers": {"llm": bool(LLM_URL), "stt": bool(STT_URL), "tts": bool(TTS_URL)},
    }


def _decision_from_completion(payload: dict, tool_names: dict[str, str]) -> MikuDecision:
    choice = payload["choices"][0]
    message = choice["message"]
    calls = message.get("tool_calls") or []
    if not calls:
        if choice.get("finish_reason") == "length":
            raise ValueError("The direct model response was incomplete")
        content = str(message.get("content") or "").strip()
        if not content:
            raise ValueError("The model returned neither a response nor a tool call")
        return MikuDecision(command="respond", argument=content[:500])
    if len(calls) != 1:
        raise ValueError("Exactly one tool call is required")
    function = calls[0]["function"]
    name = function["name"]
    raw_arguments = function.get("arguments") or {}
    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object")
    if name in tool_names:
        acknowledgement = str(arguments.pop("__miku_acknowledgement", "")).strip()[:200] or None
        result_action = str(arguments.pop("__miku_result_action", "none"))
        return MikuDecision.model_validate(
            {
                "command": "invoke",
                "acknowledgement": acknowledgement,
                "result_action": result_action,
                "integration_id": tool_names[name],
                "parameters": arguments,
            }
        )
    builtin = BUILTIN_FUNCTIONS.get(name)
    if builtin == "reference":
        return MikuDecision.model_validate({"command": arguments["action"], "argument": arguments["ref"]})
    if builtin:
        return MikuDecision.model_validate({"command": builtin})
    raise ValueError("Unknown tool call")


async def _decide_with_llm(
    request: MikuDecisionRequest,
    url: str = LLM_URL,
    model: str = LLM_MODEL,
    api_key: str = LLM_API_KEY,
) -> MikuDecision:
    system = (
        "You are MIKU, a concise, warm system companion. Either answer directly or select exactly one available "
        "function. "
        "When the user asks to show, find, list, play, open, save, remember, or otherwise interact with system "
        "content, call the closest matching module API even when the request is phrased politely or vaguely. "
        "For an ambiguous request, prefer a local API over one requiring external I/O. External read APIs are only "
        "provided when the user explicitly names their provider. "
        "Existing module APIs are authoritative. Prefer a catalog limit of 1 when one or any item is requested. "
        "Never invent IDs or call unavailable functions. Mutation functions only create a preview and still require "
        "confirmation. Respond directly for genuine conversation, explanations, or reasoning that needs no system "
        "data or action; a direct response must answer the request and must not merely promise to perform an "
        "available action. "
        "Reply in the user's language. Keep direct replies under 60 words. Do not reveal hidden chain-of-thought."
    )
    tool_names: dict[str, str] = {}
    api_tools = []
    for index, tool in enumerate(request.tools):
        name = f"api_{index}_{tool.integration_id.replace('.', '_').replace('-', '_')}"
        tool_names[name] = tool.integration_id
        parameters = dict(tool.input_schema)
        properties = dict(parameters.get("properties", {}))
        properties["__miku_acknowledgement"] = {
            "type": "string",
            "maxLength": 200,
            "description": (
                "A brief, natural acknowledgement in the user's language, such as saying you will show the results."
            ),
        }
        if tool.effect == "read":
            properties["__miku_result_action"] = {
                "type": "string",
                "enum": ["none", "open", "play"],
                "description": (
                    "What to do with the first returned result. Use play when the user asks to start media, "
                    "open when they ask to open or read it, otherwise none."
                ),
            }
        parameters["properties"] = properties
        parameters["required"] = list(properties)
        parameters["additionalProperties"] = False
        api_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (
                        f"{tool.description}. API ID: {tool.integration_id}. "
                        f"Data access: {'external network' if tool.external_io else 'local'}"
                    ),
                    "parameters": parameters,
                },
            }
        )
    reference_values = [item.ref for item in request.context]
    api_tools.extend(
        [
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
    payload = {
        "model": model,
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
        "max_tokens": 120,
        "tools": api_tools,
        "tool_choice": "auto",
    }
    endpoint = _provider_endpoint(url, "/chat/completions")
    headers = _provider_headers(api_key)
    _log_provider_request("llm.decide", endpoint, headers, payload)
    async with (
        httpx.AsyncClient(timeout=60) as client,
        client.stream(
            "POST",
            endpoint,
            headers=headers,
            json=payload,
        ) as response,
    ):
        response_payload = await _json_provider_response("llm.decide", response)
    return _decision_from_completion(response_payload, tool_names)


async def _respond_with_llm(
    request: MikuCompanionRequest,
    url: str = LLM_URL,
    model: str = LLM_MODEL,
    api_key: str = LLM_API_KEY,
) -> MikuCompanionResponse:
    system = (
        "You are MIKU, a concise, warm system companion. Write the final user-facing answer after a tool call. "
        "Use only the supplied observations as facts. Never claim an item, action, or success that is not present. "
        "The observations contain only result references and trusted module IDs; item details are rendered separately "
        "by NetSanctum. Match the user's language and continue naturally after the acknowledgement without repeating "
        "it. Say that suitable results are ready without reciting numeric counts or inventing titles. Use natural "
        "grammar and mention a requested play or open action. Avoid technical API names, Markdown, links, and hidden "
        "chain-of-thought. Return one or two complete sentences under 30 words as plain text."
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": request.model_dump_json(exclude={"fallback"}),
            },
        ],
        "temperature": 0.3,
        "max_tokens": 80,
    }
    endpoint = _provider_endpoint(url, "/chat/completions")
    headers = _provider_headers(api_key)
    _log_provider_request("llm.respond", endpoint, headers, payload)
    async with (
        httpx.AsyncClient(timeout=30) as client,
        client.stream(
            "POST",
            endpoint,
            headers=headers,
            json=payload,
        ) as response,
    ):
        response_payload = await _json_provider_response("llm.respond", response)
    choice = response_payload["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ValueError("The model response was incomplete")
    text = str(choice["message"]["content"]).strip()
    return MikuCompanionResponse(text=text[:500])


@app.post("/v1/decide", response_model=MikuDecision)
async def decide(
    body: MikuDecisionRequest,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
    provider_url: str | None = Header(None, alias="X-Miku-Provider-Url"),
    provider_model: str | None = Header(None, alias="X-Miku-Provider-Model"),
    provider_key: str | None = Header(None, alias="X-Miku-Provider-Key"),
):
    verify_runtime_token(runtime_token)
    url = (provider_url or LLM_URL).strip()
    model = (provider_model or LLM_MODEL).strip()
    key = provider_key if provider_key is not None else LLM_API_KEY
    if url:
        try:
            return await _decide_with_llm(body, url, model, key)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "MIKU LLM provider failed validation (%s); using rule planner",
                type(exc).__name__,
            )
    try:
        return plan_with_rules(body.message)
    except MikuQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/v1/respond", response_model=MikuCompanionResponse)
async def respond(
    body: MikuCompanionRequest,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
    provider_url: str | None = Header(None, alias="X-Miku-Provider-Url"),
    provider_model: str | None = Header(None, alias="X-Miku-Provider-Model"),
    provider_key: str | None = Header(None, alias="X-Miku-Provider-Key"),
):
    verify_runtime_token(runtime_token)
    url = (provider_url or LLM_URL).strip()
    model = (provider_model or LLM_MODEL).strip()
    key = provider_key if provider_key is not None else LLM_API_KEY
    if not url:
        return MikuCompanionResponse(text=body.fallback)
    try:
        return await _respond_with_llm(body, url, model, key)
    except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning(
            "MIKU LLM response synthesis failed (%s); using the server fallback",
            type(exc).__name__,
        )
        return MikuCompanionResponse(text=body.fallback)


@app.post("/v1/transcribe")
async def transcribe(
    request: Request,
    runtime_token: str = Header(..., alias="X-Miku-Runtime-Token"),
    provider_url: str | None = Header(None, alias="X-Miku-Provider-Url"),
    provider_model: str | None = Header(None, alias="X-Miku-Provider-Model"),
    provider_key: str | None = Header(None, alias="X-Miku-Provider-Key"),
):
    verify_runtime_token(runtime_token)
    content_type = request.headers.get("content-type", "").partition(";")[0].lower()
    if content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported audio type")
    url = (provider_url or STT_URL).strip()
    model = (provider_model or STT_MODEL).strip()
    key = provider_key if provider_key is not None else STT_API_KEY
    if not url:
        raise HTTPException(status_code=503, detail="STT provider is not configured")
    audio = await _bounded_request_body(request, MAX_AUDIO_BYTES)
    if not audio:
        raise HTTPException(status_code=413, detail="Audio utterance is empty")
    try:
        endpoint = _provider_endpoint(url, "/audio/transcriptions")
        headers = _provider_headers(key)
        _log_provider_request(
            "stt.transcribe",
            endpoint,
            headers,
            {"model": model, "content_type": content_type, "bytes": len(audio)},
        )
        async with (
            httpx.AsyncClient(timeout=60) as client,
            client.stream(
                "POST",
                endpoint,
                headers=headers,
                files={"file": ("utterance", audio, content_type)},
                data={"model": model},
            ) as response,
        ):
            payload = await _json_provider_response("stt.transcribe", response)
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
    provider_url: str | None = Header(None, alias="X-Miku-Provider-Url"),
    provider_model: str | None = Header(None, alias="X-Miku-Provider-Model"),
    provider_key: str | None = Header(None, alias="X-Miku-Provider-Key"),
):
    verify_runtime_token(runtime_token)
    url = (provider_url or TTS_URL).strip()
    model = (provider_model or TTS_MODEL).strip()
    key = provider_key if provider_key is not None else TTS_API_KEY
    if not url:
        raise HTTPException(status_code=503, detail="TTS provider is not configured")
    try:
        endpoint = _provider_endpoint(url, "/audio/speech")
        headers = _provider_headers(key)
        payload = {"model": model, "input": body.text, "voice": body.voice}
        _log_provider_request("tts.synthesize", endpoint, headers, payload)
        async with (
            httpx.AsyncClient(timeout=60) as client,
            client.stream(
                "POST",
                endpoint,
                headers=headers,
                json=payload,
            ) as response,
        ):
            media_type = response.headers.get("content-type", "audio/mpeg")
            audio = await _audio_provider_response("tts.synthesize", response)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="TTS provider failed") from exc
    if not audio or not media_type.lower().startswith("audio/"):
        raise HTTPException(status_code=502, detail="TTS provider returned invalid audio")
    return Response(audio, media_type=media_type)
