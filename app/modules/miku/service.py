from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.library_viewer_v1 import CONTRACT_ID, LibraryResult
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationServiceError,
    IntegrationUnavailableError,
)
from app.modules.miku.schemas import MikuCapabilities, MikuProvider, MikuQuery, MikuReference, MikuReply

MikuCommand = Literal["help", "sources", "list", "find"]
COMMANDS = ("help", "sources", "list [module]", "find <text>")
COMMAND_ALIASES: dict[str, MikuCommand] = {
    "help": "help",
    "помощь": "help",
    "sources": "sources",
    "источники": "sources",
    "list": "list",
    "список": "list",
    "find": "find",
    "search": "find",
    "найди": "find",
    "поиск": "find",
}
FIND_SCAN_LIMIT = 50


class MikuQueryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class _Provider:
    module_id: str
    integration_id: str


class _Registry(Protocol):
    def integration_catalog(self, consumer_id: str | None = None) -> list[dict[str, Any]]: ...

    async def invoke_integration(
        self,
        integration_id: str,
        payload: dict[str, Any],
        context: IntegrationContext,
    ) -> dict[str, Any]: ...


def _providers(registry: _Registry) -> list[_Provider]:
    catalog = registry.integration_catalog(consumer_id="miku")
    return [
        _Provider(module_id=item["module_id"], integration_id=item["id"])
        for item in catalog
        if item["contract"] == CONTRACT_ID and item["effects"]["effect"] == "read"
    ]


def capabilities(registry: _Registry) -> MikuCapabilities:
    return MikuCapabilities(
        commands=list(COMMANDS),
        providers=[
            MikuProvider(module_id=provider.module_id, integration_id=provider.integration_id)
            for provider in _providers(registry)
        ],
    )


def _parse(message: str) -> tuple[MikuCommand, str]:
    parts = message.split(maxsplit=1)
    command_text = parts[0]
    argument = parts[1] if len(parts) == 2 else ""
    command = COMMAND_ALIASES.get(command_text.casefold())
    if command is None:
        raise MikuQueryError("Unknown command. Use help to list available commands.")
    argument = argument.strip()
    if command == "find" and not argument:
        raise MikuQueryError("The find command requires search text.")
    if command in {"help", "sources"} and argument:
        raise MikuQueryError(f"The {command} command does not accept arguments.")
    return command, argument


def _trim(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _reference(module_id: str, item, index: int) -> MikuReference:
    return MikuReference(
        ref=f"result:{index}",
        module_id=module_id,
        item_id=str(item.id),
        kind=_trim(item.kind, 64) or "item",
        title=_trim(item.title, 160) or "Untitled",
        subtitle=_trim(item.subtitle, 160),
        summary=_trim(item.description, 300),
        playable=bool(item.playable),
        readable=bool(item.readable),
    )


async def _catalog(
    provider: _Provider,
    limit: int,
    db: AsyncSession,
    user,
    registry: _Registry,
) -> LibraryResult:
    result = await registry.invoke_integration(
        provider.integration_id,
        {"operation": "catalog", "limit": limit, "offset": 0},
        IntegrationContext(session=db, user=user, registry=registry, consumer_id="miku"),
    )
    catalog = LibraryResult.model_validate(result)
    if catalog.module_id != provider.module_id:
        raise IntegrationRejectedError("Provider returned the wrong module ID")
    return catalog


async def query(
    request: MikuQuery,
    db: AsyncSession,
    user,
    registry: _Registry,
) -> MikuReply:
    command, argument = _parse(request.message)
    providers = _providers(registry)

    if command == "help":
        return MikuReply(
            command=command,
            text="Commands: help, sources, list [module], find <text>.",
        )
    if command == "sources":
        names = ", ".join(provider.module_id for provider in providers) or "none"
        return MikuReply(command=command, text=f"Read-only sources: {names}.")

    selected = providers
    if command == "list" and argument:
        selected = [provider for provider in providers if provider.module_id == argument.casefold()]
        if not selected:
            raise MikuQueryError(f"Unknown or unavailable source: {argument}")

    references: list[MikuReference] = []
    warnings: list[str] = []
    search_text = argument.casefold()
    for provider in selected:
        try:
            catalog = await _catalog(
                provider,
                FIND_SCAN_LIMIT if command == "find" else request.limit,
                db,
                user,
                registry,
            )
        except (
            IntegrationNotFoundError,
            IntegrationRejectedError,
            IntegrationServiceError,
            IntegrationUnavailableError,
            ValidationError,
        ):
            warnings.append(f"{provider.module_id} is unavailable")
            continue
        for item in catalog.items:
            if command == "find":
                searchable = " ".join(
                    value for value in (item.title, item.subtitle, item.description) if value
                ).casefold()
                if search_text not in searchable:
                    continue
            references.append(_reference(provider.module_id, item, len(references) + 1))
            if len(references) >= request.limit:
                break
        if len(references) >= request.limit:
            break

    action = "Found" if command == "find" else "Listed"
    return MikuReply(
        command=command,
        text=f"{action} {len(references)} item(s).",
        references=references,
        warnings=warnings,
    )
