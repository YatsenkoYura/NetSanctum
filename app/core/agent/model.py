"""OpenAI-compatible model client used by the cascade engine.

Small local models are the reason this file is deliberately dumb: one system prompt,
one JSON state block, tool calls forced to be real tool calls, and no prose fallback.
"""

import json
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.agent.catalog import AgentTool
from app.core.agent.engine import AgentStep, AgentUnavailableError
from app.core.agent.primitives import PRIMITIVE_NAMES
from app.core.agent.urls import CHAT_COMPLETIONS, provider_endpoint

SYSTEM_PROMPT = (
    "You are the reasoning step of a personal assistant. You never answer in prose: "
    "every reply is exactly one tool call.\n"
    "Work like this: search when you do not know where something is, read the content of a "
    "result before you describe or summarise it, fetch a URL when the answer lives outside "
    "the local library, and act only when the user asked to open or play something.\n"
    "Rules: call final exactly once to end the turn, using only facts from the executed "
    "steps; call ask when the request is ambiguous and you cannot proceed; never invent "
    "result references; never repeat a call that already succeeded.\n"
    "After act succeeds, call final straight away: the user already sees the media.\n"
    "The failed_steps list holds tools that already failed: never call them again, "
    "choose a different tool or answer with what you have.\n"
    "The history holds only the most recent turns, not the whole conversation. When "
    "something still matters after those turns scroll away - the subject being "
    "discussed, a preference the user stated, a decision already made - write it down "
    "with the conversation note tool instead of relying on remembering it, and read the "
    "notes back when you need it.\n"
    "Reason in at most one short sentence, then call exactly one tool.\n"
    "Keep the final answer to at most four short sentences."
)
TRUNCATION_NUDGE = (
    "Your previous reply was cut off before the tool call. Do not explain: call exactly one tool right now. "
    "A tool call is not complete until its arguments carry the values: an empty {} is rejected."
)
# A model that produced something unusable gets one more try. A small local model
# commonly names the right tool and then hands back empty or unparsable arguments,
# which is no more a provider failure than running out of budget mid-sentence is.
RETRYABLE_REPLIES = (
    "neither a tool call nor an answer",
    "tool arguments were empty",
    "tool arguments were not JSON",
)
MAX_RESPONSE_BYTES = 256 * 1024
MAX_PROSE_ANSWER = 2_000
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 700
# A reasoning model spends part of its budget thinking before it emits anything, so a
# reply that fits a normal model can arrive with an empty answer. The retry widens the
# budget instead of only nudging the model, otherwise the second attempt thinks itself
# into the same wall and the turn fails as if the provider were down.
RETRY_MAX_TOKENS = 4_096


class OpenAICompatibleModel:
    def __init__(
        self,
        url: str,
        model: str,
        *,
        api_key: str = "",
        timeout: float = 240.0,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        thinking: bool = True,
        transport: Any | None = None,
    ) -> None:
        # A saved provider URL is a base; providers speak full endpoints.
        self.url = provider_endpoint(url, CHAT_COMPLETIONS)
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _api_tools(self, tools: list[AgentTool]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def _payload(self, message: str, tools: list[AgentTool], state: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "temperature": DEFAULT_TEMPERATURE,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {**state, "request": message, "tools": [tool.name for tool in tools]},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "tools": self._api_tools(tools),
            "tool_choice": "required",
        }
        if not self.thinking:
            # A reasoning model burns its budget on hidden thinking and can return an
            # empty answer. Local servers honour this; hosted providers never see it
            # because they keep the default.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    async def next_step(
        self,
        message: str,
        tools: list[AgentTool],
        state: dict[str, Any],
    ) -> AgentStep:
        usable = [tool for tool in tools if tool.name in PRIMITIVE_NAMES or tool.integration_id]
        if not usable:
            raise AgentUnavailableError("no tools available")
        known = {tool.name for tool in usable}
        for attempt in (1, 2):
            payload = self._payload(message, usable, state)
            if attempt == 2:
                payload["messages"][0]["content"] += f"\n\n{TRUNCATION_NUDGE}"
                payload["max_tokens"] = max(self.max_tokens * 2, RETRY_MAX_TOKENS)
            completion = await self._complete(payload)
            try:
                return step_from_completion(completion, known)
            except AgentUnavailableError as exc:
                # A reasoning model can spend the whole budget thinking and send nothing.
                if attempt == 1 and any(reason in str(exc) for reason in RETRYABLE_REPLIES):
                    continue
                raise
        raise AgentUnavailableError("the model did not return a usable tool call")

    async def _complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(self.url, headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise AgentUnavailableError(f"model transport failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            # Provider errors are the only way to learn about a context or schema limit.
            detail = response.text[:300].replace("\n", " ")
            raise AgentUnavailableError(f"model returned {response.status_code}: {detail}")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise AgentUnavailableError("model response too large")
        return response.json()


def step_from_completion(payload: dict[str, Any], known_tools: set[str]) -> AgentStep:
    """Parse one tool call into a validated step, refusing anything ambiguous."""
    try:
        choices = payload["choices"]
        message = choices[0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AgentUnavailableError("malformed completion") from exc
    calls = message.get("tool_calls") or []
    queued: list[dict[str, Any]] = []
    if len(calls) > 1:
        # A model may want to answer and act at once: the terminal call goes first.
        terminal = next(
            (item for item in calls if (item.get("function") or {}).get("name") in {"final", "ask"}),
            None,
        )
        if terminal is not None:
            queued = [item for item in calls if item is not terminal]
            calls = [terminal]
        else:
            # Otherwise run them in order: the engine executes one per step.
            queued = list(calls[1:])
    if not calls:
        # Some local servers ignore tool_choice and answer in prose. Treat it as the
        # answer: the engine still validates it against what the steps produced.
        content = str(message.get("content") or "").strip()
        if not content:
            raise AgentUnavailableError("the model returned neither a tool call nor an answer")
        return AgentStep(kind="final", tool="final", arguments={"answer": content[:MAX_PROSE_ANSWER]})
    call = calls[0] or {}
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    if name not in known_tools:
        raise AgentUnavailableError(f"the model called an unknown tool {name!r}")
    raw = function.get("arguments")
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        raise AgentUnavailableError("tool arguments were not JSON") from exc
    if not isinstance(arguments, dict) or not arguments:
        raise AgentUnavailableError("tool arguments were empty")
    kind = "tool"
    if name == "final":
        kind = "final"
    elif name == "ask":
        kind = "ask"
    try:
        return AgentStep(
            kind=kind,
            tool=name,
            arguments=arguments,
            queued=[_queued_call(item, known_tools) for item in queued],
        )
    except ValidationError as exc:
        raise AgentUnavailableError(f"invalid arguments for {name}: {exc}") from exc


def _queued_call(call: dict[str, Any], known_tools: set[str]) -> dict[str, Any]:
    """Normalise an extra tool call so the engine can run it on the next step."""
    function = (call or {}).get("function") or {}
    name = str(function.get("name") or "")
    if name not in known_tools:
        raise AgentUnavailableError(f"the model called an unknown tool {name!r}")
    raw = function.get("arguments")
    arguments = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(arguments, dict) or not arguments:
        raise AgentUnavailableError("tool arguments were empty")
    return {"tool": name, "arguments": arguments}


__all__ = ["DEFAULT_TEMPERATURE", "MAX_RESPONSE_BYTES", "OpenAICompatibleModel", "step_from_completion"]
