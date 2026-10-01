"""Tool catalog for the agent: fixed primitives plus every declared integration.

Nothing here knows about individual modules. A new module integration becomes an
agent tool automatically, and the tool schema is the integration's own request model.
"""

import re
from typing import Any

from pydantic import BaseModel, Field

from app.core.agent.primitives import (
    MAX_ANSWER_LENGTH,
    MAX_ANSWER_REFS,
    MAX_OPTION_LENGTH,
    MAX_QUESTION_LENGTH,
    READ_TEXT_LIMIT,
    AgentActRequest,
    AgentAskRequest,
    AgentFetchRequest,
    AgentReadRequest,
)

TOOL_NAME_PATTERN = re.compile(r"[^a-z0-9_]+")
# A small model pays for every token of the catalog, on every single step.
MAX_TOOL_DESCRIPTION = 200
MAX_ENUM_VALUES = 8
# Property help the model actually needs to pick arguments. Kept short on purpose.
MAX_PROPERTY_DESCRIPTION = 120

PRIMITIVE_DESCRIPTIONS: dict[str, str] = {
    "read": (
        "Read the actual content behind a result from an earlier step: chapter text, page text, "
        "subtitle or description. Use it before answering questions about what a result contains."
    ),
    "fetch": ("Read a public https page as text. Use it when the answer lives outside the local library."),
    "act": "Show a result to the user: open it on screen, or start playing it.",
    "ask": "Ask the user a short question when the request is ambiguous. Ends the turn.",
    "final": (
        "Give the final answer from executed steps only. Answer briefly like MIKU: warm, "
        "max four short sentences, one emoji at most, never narrate an action the client "
        "already performs."
    ),
}


def tool_name(integration_id: str) -> str:
    """Stable, model-friendly tool name for an integration id."""
    return TOOL_NAME_PATTERN.sub("_", integration_id.casefold()).strip("_")


def _strip_schema_noise(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep tool schemas small: models follow titles and long descriptions poorly.

    Property *names* are kept, plus one short hint per property so the model knows
    what each argument means. Without it the model guesses filters blindly.
    """
    keywords = {"type", "properties", "required", "additionalProperties", "enum", "items", "description"}

    def walk(node: Any) -> Any:
        if not isinstance(node, dict):
            return node
        compacted = {key: value for key, value in node.items() if key in keywords}
        if isinstance(compacted.get("description"), str):
            compacted["description"] = " ".join(compacted["description"].split())[:MAX_PROPERTY_DESCRIPTION]
        properties = compacted.get("properties")
        if isinstance(properties, dict):
            compacted["properties"] = {name: walk(sub) for name, sub in properties.items()}
        if isinstance(compacted.get("items"), dict):
            compacted["items"] = walk(compacted["items"])
        if isinstance(compacted.get("enum"), list):
            compacted["enum"] = list(compacted["enum"])[:MAX_ENUM_VALUES]
        return compacted

    return walk(schema) or {"type": "object", "properties": {}}


class AgentTool(BaseModel):
    name: str = Field(max_length=128, pattern=r"^[a-z][a-z0-9_]{0,127}$")
    description: str = Field(max_length=MAX_TOOL_DESCRIPTION)
    parameters: dict[str, Any]
    kind: str = Field(default="integration", max_length=16)
    integration_id: str | None = Field(default=None, max_length=128)
    contract: str | None = Field(default=None, max_length=128)
    effect: str = Field(default="read", max_length=16)
    external_io: bool = False
    reversible: bool = False


def _primitive_tools() -> list[AgentTool]:
    return [
        AgentTool(
            name="read",
            description=PRIMITIVE_DESCRIPTIONS["read"],
            parameters=_strip_schema_noise(AgentReadRequest.model_json_schema()),
            kind="primitive",
        ),
        AgentTool(
            name="fetch",
            description=PRIMITIVE_DESCRIPTIONS["fetch"],
            parameters=_strip_schema_noise(AgentFetchRequest.model_json_schema()),
            kind="primitive",
        ),
        AgentTool(
            name="act",
            description=PRIMITIVE_DESCRIPTIONS["act"],
            parameters=_strip_schema_noise(AgentActRequest.model_json_schema()),
            kind="primitive",
        ),
        AgentTool(
            name="ask",
            description=PRIMITIVE_DESCRIPTIONS["ask"],
            parameters=_strip_schema_noise(AgentAskRequest.model_json_schema()),
            kind="primitive",
        ),
        AgentTool(
            name="final",
            description=PRIMITIVE_DESCRIPTIONS["final"],
            parameters=_final_schema(),
            kind="primitive",
            effect="execute",
        ),
    ]


def _final_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "maxLength": MAX_ANSWER_LENGTH},
            "refs": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ANSWER_REFS},
        },
        "required": ["answer"],
        "additionalProperties": False,
    }


def _describe(description: str) -> str:
    """One short sentence per tool: enough to choose it, cheap enough to send always."""
    sentence = " ".join(description.split())
    if len(sentence) <= MAX_TOOL_DESCRIPTION:
        return sentence
    return f"{sentence[:MAX_TOOL_DESCRIPTION].rsplit(' ', 1)[0]}."


def integration_tools(catalog: list[dict[str, Any]]) -> list[AgentTool]:
    """Turn an integration catalog into agent tools, one per declared integration."""
    tools = []
    for item in catalog:
        effects = item.get("effects") or {}
        description = (item.get("description") or "").strip()
        tools.append(
            AgentTool(
                name=tool_name(item["id"]),
                description=_describe(description or f"Call the {item['id']} integration."),
                parameters=_strip_schema_noise(item.get("request_schema") or {}),
                kind="integration",
                integration_id=item["id"],
                contract=(item.get("contract") or None),
                effect=effects.get("effect", "execute"),
                external_io=bool(effects.get("external_io")),
                reversible=bool(effects.get("reversible", False)),
            )
        )
    return tools


def build_tool_catalog(catalog: list[dict[str, Any]]) -> list[AgentTool]:
    """Fixed primitives first, then integrations: local private reads before external I/O.

    A small model picks the first plausible tool, so order is policy. Reads without
    external I/O come first, mutating and remote tools last.
    """
    tools = _primitive_tools()
    integrations = integration_tools(catalog)
    integrations.sort(key=lambda tool: (tool.effect != "read", tool.external_io, tool.name))
    tools.extend(integrations)
    return tools


__all__ = [
    "MAX_OPTION_LENGTH",
    "MAX_QUESTION_LENGTH",
    "READ_TEXT_LIMIT",
    "AgentTool",
    "build_tool_catalog",
    "integration_tools",
    "tool_name",
]
