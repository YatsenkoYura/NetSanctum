"""The cascade engine: skeleton first, arguments per step, one grounded final answer.

Design rules that keep a small local model usable:

* every decision is a tool call, so the wire format is uniform for the model;
* the model picks the *next* tool freely, but must always call something;
* arguments are produced against the real results of previous steps, never planned ahead;
* ``final`` and ``ask`` are terminal tools, so the loop cannot leak an empty answer;
* budgets are enforced by the engine, not by the model, and exhaustion is explained out loud.
"""

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field, model_validator

from app.core.agent.catalog import AgentTool
from app.core.agent.fetch import RemoteFetchError, fetch_public_text
from app.core.agent.primitives import (
    AgentActRequest,
    AgentAskRequest,
    AgentFetchRequest,
    AgentFinalRequest,
    AgentReadRequest,
    AgentStep,
)
from app.core.agent.references import AgentReference, project_references

MAX_MESSAGE_LENGTH = 2_000
MAX_STEPS = 12
STEP_RESULT_TEXT_LIMIT = 3_000
MAX_EXECUTED_STEPS = 12
FREE_FORM_STEPS = 3
STAGNATION_STEPS = 2
MAX_STATE_REFERENCES = 6
STATE_TITLE_LIMIT = 60
# Recent turns shown to the model verbatim. The full transcript is stored and can be
# far longer; anything older is expected to live in what the agent wrote down.
HISTORY_TURNS = 6


class AgentBudget(BaseModel):
    max_steps: int = Field(default=6, ge=1, le=MAX_STEPS)
    wall_clock_seconds: float = Field(default=180.0, ge=5.0, le=900.0)
    max_fetch_bytes: int = Field(default=8 * 1024 * 1024, ge=64 * 1024)


class AgentProviderProfile(BaseModel):
    """Provider credentials for one call: the runtime stores no secrets of its own."""

    url: str = Field(default="", max_length=1_000)
    model: str = Field(default="", max_length=200)
    api_key: str = Field(default="", max_length=500)
    mode: str = Field(default="api", max_length=16)
    server_callable: bool = True


class AgentHistoryTurn(BaseModel):
    user: str = Field(max_length=MAX_MESSAGE_LENGTH)
    assistant: str = Field(max_length=MAX_MESSAGE_LENGTH)


class AgentTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    session_id: str = Field(default="", max_length=64)
    history: list[AgentHistoryTurn] = Field(default_factory=list, max_length=HISTORY_TURNS)
    references: list[AgentReference] = Field(default_factory=list, max_length=20)
    budget: AgentBudget = Field(default_factory=AgentBudget)
    llm: AgentProviderProfile | None = None


class AgentStepRecord(BaseModel):
    tool: str = Field(max_length=128)
    status: str = Field(max_length=16)
    summary: str = Field(max_length=300)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reference_refs: list[str] = Field(default_factory=list, max_length=20)
    text: str | None = Field(default=None, max_length=STEP_RESULT_TEXT_LIMIT)


class AgentTurnResult(BaseModel):
    answer: str = Field(default="", max_length=2_000)
    mood: str = Field(default="neutral", max_length=16)
    refs: list[str] = Field(default_factory=list, max_length=20)
    client_action: str | None = Field(default=None, max_length=8)
    question: str | None = Field(default=None, max_length=300)
    question_options: list[str] = Field(default_factory=list, max_length=4)
    steps: list[AgentStepRecord] = Field(default_factory=list, max_length=MAX_EXECUTED_STEPS)
    skeleton: list[str] = Field(default_factory=list, max_length=MAX_STEPS)
    references: list[AgentReference] = Field(default_factory=list, max_length=20)
    exhausted: bool = False

    @model_validator(mode="after")
    def validate_outcome(self):
        if not self.answer and not self.question:
            raise ValueError("A turn must end with an answer or a question")
        return self


class AgentUnavailableError(RuntimeError):
    """The model could not be reached or did not return a usable tool call."""


class ModelClient(Protocol):
    """Anything that can turn the cascade state into one tool call."""

    async def next_step(
        self,
        message: str,
        tools: list[AgentTool],
        state: dict[str, Any],
    ) -> AgentStep: ...


class ToolBackend(Protocol):
    """The application's side of the world, reached over the internal contract."""

    async def catalog(self) -> list[AgentTool]: ...

    async def invoke(self, integration_id: str, parameters: dict[str, Any]) -> dict[str, Any]: ...

    async def read(self, module_id: str, item_id: str, max_chars: int) -> dict[str, Any]: ...


ProgressHook = Callable[[str, dict[str, Any]], Awaitable[None]]


def _noop_progress(phase: str, data: dict[str, Any]) -> Awaitable[None]:
    async def call() -> None:
        return None

    return call()


class CascadeEngine:
    """Runs one turn: model picks a step, the engine executes it, repeat."""

    def __init__(self, model: ModelClient, backend: ToolBackend) -> None:
        self.model = model
        self.backend = backend

    async def run(
        self,
        request: AgentTurnRequest,
        *,
        on_progress: ProgressHook | None = None,
    ) -> AgentTurnResult:
        started = time.monotonic()
        progress = on_progress or _noop_progress
        tools = await self.backend.catalog()
        by_name = {tool.name: tool for tool in tools}
        references: list[AgentReference] = list(request.references)
        executed: list[dict[str, Any]] = []
        records: list[AgentStepRecord] = []
        skeleton: list[str] = []
        seen: set[str] = set()
        failed: set[str] = set()
        pending: list[dict[str, Any]] = []
        learned: list[bool] = []
        fetch_bytes = 0
        final: AgentFinalRequest | None = None
        final_from_model = False
        question: AgentAskRequest | None = None
        client_action: str | None = None

        for index in range(request.budget.max_steps):
            if time.monotonic() - started > request.budget.wall_clock_seconds:
                final = self._exhausted("Я не успела закончить за отведённое время.")
                break
            closing = not self._can_keep_working(index, learned)
            state = self._state(request, references, executed, skeleton, failed)
            offered = _closing_tools(tools) if closing else tools
            try:
                if pending and not closing:
                    # The model asked for several things at once: run them in order.
                    queued_call = pending.pop(0)
                    step = AgentStep(
                        kind="tool",
                        tool=str(queued_call["tool"]),
                        arguments=dict(queued_call.get("arguments") or {}),
                    )
                else:
                    step = await self.model.next_step(request.message, offered, state)
            except AgentUnavailableError as exc:
                final = self._exhausted("Модель сейчас недоступна, я не смогла закончить мысль.")
                await progress("error", {"reason": str(exc)[:200]})
                break
            if step.queued and not closing:
                pending.extend(step.queued)
            if step.scratchpad and not skeleton:
                skeleton = self._skeleton_from_scratchpad(step.scratchpad)
            if step.kind == "final":
                final = AgentFinalRequest.model_validate(step.arguments)
                final_from_model = True
                break
            if step.kind == "ask":
                question = AgentAskRequest.model_validate(step.arguments)
                break
            tool = by_name.get(step.tool or "")
            if tool is not None and tool.name not in {offered_tool.name for offered_tool in offered}:
                # The turn is winding down: only the closing tools are still callable.
                learned.append(False)
                records.append(
                    AgentStepRecord(
                        tool=str(step.tool)[:128],
                        status="error",
                        summary="Этот инструмент больше не доступен в этом ходе, пора завершать.",
                        arguments=step.arguments,
                    )
                )
                executed.append(
                    {
                        "tool": step.tool,
                        "status": "error",
                        "error": "tool withdrawn, the turn is closing",
                    }
                )
                continue
            if tool is None:
                learned.append(False)
                records.append(
                    AgentStepRecord(
                        tool=str(step.tool)[:128],
                        status="error",
                        summary="Инструмент недоступен в этом ходе.",
                    )
                )
                executed.append({"tool": step.tool, "status": "error", "error": "unknown tool"})
                continue
            signature = json.dumps([tool.name, step.arguments], sort_keys=True, default=str)
            if signature in seen:
                learned.append(False)
                records.append(
                    AgentStepRecord(
                        tool=tool.name,
                        status="skipped",
                        summary="Этот шаг уже выполнен, повтор не нужен.",
                        arguments=step.arguments,
                    )
                )
                continue
            seen.add(signature)
            record, new_references, outcome = await self._execute(
                tool,
                step.arguments,
                references,
                remaining_bytes=max(0, request.budget.max_fetch_bytes - fetch_bytes),
            )
            if outcome.get("bytes"):
                fetch_bytes += int(outcome["bytes"])
            before = len(references)
            references = self._merge_references(references, new_references)
            added = len(references) - before
            records.append(record)
            executed.append(outcome)
            await progress(
                "step",
                {
                    "tool": tool.name,
                    "status": record.status,
                    "summary": record.summary,
                    "result_count": len(new_references),
                },
            )
            if outcome.get("client_action"):
                client_action = str(outcome["client_action"])
            # A step is only progress if it brought back something the model has not seen.
            # Progress means the model learned something it did not already have.
            learned.append(added > 0 or bool(outcome.get("text")))
            if outcome.get("status") == "error":
                failed.add(tool.name)

        if final is None and question is None:
            final = self._exhausted("Я исчерпала шаги и останавливаюсь.")
            exhausted = True
        else:
            exhausted = False

        # Only attach results the model itself did not mention when the model spoke:
        # an engine-generated message must not claim to be about them.
        if final is not None and final_from_model and not final.refs:
            final.refs = [reference.ref for reference in references[:3]]

        return AgentTurnResult(
            answer=final.answer if final else "",
            refs=list(final.refs) if final else [],
            client_action=client_action,
            question=question.question if question else None,
            question_options=list(question.options) if question else [],
            steps=records,
            skeleton=skeleton,
            references=references,
            exhausted=exhausted,
        )

    @staticmethod
    def _can_keep_working(index: int, progress: list[bool]) -> bool:
        """Stop offering open-ended tools once the turn stops making progress.

        A small model will happily search forever. After a few free steps, or after two
        steps in a row that learned nothing new, only the closing tools are left.
        """
        if index < FREE_FORM_STEPS:
            return True
        return all(progress[-STAGNATION_STEPS:])

    def _state(
        self,
        request: AgentTurnRequest,
        references: list[AgentReference],
        executed: list[dict[str, Any]],
        skeleton: list[str],
        failed: set[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "goal": request.message,
            "conversation": [turn.model_dump(mode="json") for turn in request.history[-HISTORY_TURNS:]],
            # Compacted on purpose: a local model pays for every token of every card.
            "references": [_compact_reference(reference) for reference in references[:MAX_STATE_REFERENCES]],
            "executed": executed[-MAX_EXECUTED_STEPS:],
            "skeleton": skeleton,
            "failed_steps": sorted(failed or set()),
        }

    @staticmethod
    def _exhausted(reason: str) -> AgentFinalRequest:
        return AgentFinalRequest(answer=reason)

    @staticmethod
    def _skeleton_from_scratchpad(scratchpad: str) -> list[str]:
        steps = [part.strip() for part in scratchpad.replace("→", ",").split(",")]
        return [step[:64] for step in steps if step][:MAX_STEPS]

    @staticmethod
    def _merge_references(
        existing: list[AgentReference],
        new_references: list[AgentReference],
    ) -> list[AgentReference]:
        """Keep numbering stable so a ref stays valid for the rest of the turn."""
        if not new_references:
            return existing
        known = {(item.module_id, item.item_id) for item in existing}
        merged = list(existing)
        for reference in new_references:
            if (reference.module_id, reference.item_id) in known:
                continue
            known.add((reference.module_id, reference.item_id))
            merged.append(reference.model_copy(update={"ref": f"result:{len(merged) + 1}"}))
        return merged[:20]

    async def _execute(
        self,
        tool: AgentTool,
        arguments: dict[str, Any],
        references: list[AgentReference],
        *,
        remaining_bytes: int,
    ) -> tuple[AgentStepRecord, list[AgentReference], dict[str, Any]]:
        name = tool.name
        try:
            if name == "read":
                return await self._read(arguments, references)
            if name == "fetch":
                return await self._fetch(arguments, remaining_bytes)
            if name == "act":
                return self._act(arguments, references)
            if not tool.integration_id:
                raise ValueError("not a callable tool")
            parameters = _validate(tool, arguments)
            result = await self.backend.invoke(tool.integration_id, parameters)
            found = project_references(tool.contract, tool.integration_id or tool.name, result)
            summary = f"Найдено результатов: {len(found)}." if found else "Ничего не найдено."
            record = AgentStepRecord(
                tool=name,
                status="success" if found else "empty",
                summary=summary,
                arguments=arguments,
                reference_refs=[reference.ref for reference in found],
            )
            outcome = {
                "tool": name,
                "status": record.status,
                "summary": summary,
                "arguments": arguments,
                "references": [reference.model_dump(mode="json") for reference in found],
            }
            return record, found, outcome
        except (ValueError, RemoteFetchError, LookupError, RuntimeError) as exc:
            reason = str(exc)[:200]
            record = AgentStepRecord(
                tool=name,
                status="error",
                summary=f"Шаг не удался: {reason}",
                arguments=arguments,
            )
            return record, [], {"tool": name, "status": "error", "error": reason, "arguments": arguments}

    async def _read(
        self,
        arguments: dict[str, Any],
        references: list[AgentReference],
    ) -> tuple[AgentStepRecord, list[AgentReference], dict[str, Any]]:
        request = AgentReadRequest.model_validate(arguments)
        reference = _find_reference(request.ref, references)
        if reference is None:
            raise ValueError(f"Unknown reference {request.ref}")
        payload = await self.backend.read(reference.module_id, reference.item_id, request.max_chars)
        text = str(payload.get("text") or "")[:STEP_RESULT_TEXT_LIMIT]
        kind = str(payload.get("kind") or "text")
        if not text:
            if kind == "text":
                raise ValueError("this result has no text to read")
            # Media has no text: say so plainly so the model stops retrying the read.
            summary = f"{kind} без текста: {payload.get('title') or reference.title}"
            record = AgentStepRecord(
                tool="read",
                status="empty",
                summary=summary,
                arguments=arguments,
            )
            return (
                record,
                [],
                {
                    "tool": "read",
                    "status": "empty",
                    "summary": summary,
                    "ref": request.ref,
                    "kind": kind,
                    "hint": "Use act to play or open it, or final to answer from what you have.",
                },
            )
        truncated = bool(payload.get("truncated"))
        summary = f"Прочитала: {payload.get('title') or reference.title}"
        record = AgentStepRecord(
            tool="read",
            status="success",
            summary=summary,
            arguments=arguments,
            text=text,
        )
        outcome = {
            "tool": "read",
            "status": "success",
            "summary": summary,
            "ref": request.ref,
            "title": payload.get("title") or reference.title,
            "text": text,
            "truncated": truncated,
        }
        return record, [], outcome

    async def _fetch(
        self,
        arguments: dict[str, Any],
        remaining_bytes: int,
    ) -> tuple[AgentStepRecord, list[AgentReference], dict[str, Any]]:
        request = AgentFetchRequest.model_validate(arguments)
        if remaining_bytes < 64 * 1024:
            raise ValueError("Fetch budget for this turn is spent")
        result = await fetch_public_text(
            request.url,
            max_chars=request.max_chars,
            max_bytes=min(remaining_bytes, request.max_chars * 8),
        )
        summary = f"Открыла {result.final_url}"
        record = AgentStepRecord(
            tool="fetch",
            status="success",
            summary=summary,
            arguments=arguments,
            text=result.text[:STEP_RESULT_TEXT_LIMIT],
        )
        outcome = {
            "tool": "fetch",
            "status": "success",
            "summary": summary,
            "url": result.final_url,
            "title": result.title,
            "text": result.text[:STEP_RESULT_TEXT_LIMIT],
            "truncated": result.truncated,
            "bytes": result.bytes_read,
        }
        return record, [], outcome

    @staticmethod
    def _act(
        arguments: dict[str, Any],
        references: list[AgentReference],
    ) -> tuple[AgentStepRecord, list[AgentReference], dict[str, Any]]:
        request = AgentActRequest.model_validate(arguments)
        reference = _find_reference(request.ref, references)
        if reference is None:
            raise ValueError(f"Unknown reference {request.ref}")
        target = reference.open_url
        if request.action == "play" and not reference.playable:
            raise ValueError("This result cannot be played")
        if not target:
            raise ValueError("This result has nowhere to open")
        summary = f"{'Запускаю' if request.action == 'play' else 'Открываю'}: {reference.title}"
        record = AgentStepRecord(
            tool="act",
            status="success",
            summary=summary,
            arguments=arguments,
            reference_refs=[reference.ref],
        )
        outcome = {
            "tool": "act",
            "status": "success",
            "summary": summary,
            "ref": reference.ref,
            "action": request.action,
            "client_action": request.action,
            "target": target,
            "title": reference.title,
        }
        return record, [], outcome


def _closing_tools(tools: list[AgentTool]) -> list[AgentTool]:
    """The only tools a stalled turn may still use: wrap up or ask."""
    return [tool for tool in tools if tool.name in {"final", "ask"}]


def _compact_reference(reference: AgentReference) -> dict[str, Any]:
    return {
        "ref": reference.ref,
        "module": reference.module_id,
        "id": reference.item_id,
        "kind": reference.kind,
        "title": reference.title[:STATE_TITLE_LIMIT],
        "playable": reference.playable,
        "readable": reference.readable,
    }


def _find_reference(ref: str, references: list[AgentReference]) -> AgentReference | None:
    return next((reference for reference in references if reference.ref == ref), None)


def _validate(tool: AgentTool, arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate arguments against the tool's own JSON schema, required fields included."""
    schema = tool.parameters or {}
    required = schema.get("required") or []
    missing = [name for name in required if name not in arguments]
    if missing:
        raise ValueError(f"missing arguments: {', '.join(missing[:3])}")
    properties = schema.get("properties") or {}
    for name, value in arguments.items():
        expected = properties.get(name)
        if not expected:
            continue
        expected_type = expected.get("type")
        if expected_type == "string" and not isinstance(value, str):
            raise ValueError(f"{name} must be text")
        if expected_type == "integer" and not isinstance(value, int):
            raise ValueError(f"{name} must be a number")
        if expected_type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        if expected_type == "array" and not isinstance(value, list):
            raise ValueError(f"{name} must be a list")
        choices = expected.get("enum")
        if choices and value not in choices:
            raise ValueError(f"{name} must be one of: {', '.join(str(choice) for choice in choices[:4])}")
    return arguments


__all__ = [
    "AgentBudget",
    "AgentHistoryTurn",
    "AgentProviderProfile",
    "AgentStepRecord",
    "AgentTurnRequest",
    "AgentTurnResult",
    "AgentUnavailableError",
    "CascadeEngine",
    "ModelClient",
    "ToolBackend",
]
