import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.contracts.library_viewer_v1 import CONTRACT_ID, LibraryResult
from app.contracts.vault_capture_v1 import VaultCaptureRequest
from app.contracts.video_source_catalog_v1 import VideoSourceResult
from app.core.config import get_settings
from app.core.control_center import tracked_tasks
from app.core.module_types import (
    IntegrationContext,
    IntegrationNotFoundError,
    IntegrationRejectedError,
    IntegrationResource,
    IntegrationServiceError,
    IntegrationUnavailableError,
)
from app.modules.miku.models import MikuTurnAudit
from app.modules.miku.planner import MikuQueryError
from app.modules.miku.runtime_client import miku_runtime_client
from app.modules.miku.schemas import (
    MikuActionResult,
    MikuCapabilities,
    MikuContextReference,
    MikuDecision,
    MikuJobStatus,
    MikuPendingAction,
    MikuProvider,
    MikuQuery,
    MikuReference,
    MikuReply,
    MikuToolDefinition,
)

COMMANDS = (
    "help",
    "sources",
    "list [module]",
    "find <text>",
    "discover <text>",
    "open result:N",
    "play result:N",
    "archive result:N",
    "note <text>",
    "bookmark <url>",
    "repeat",
)
FIND_SCAN_LIMIT = 50
SOURCE_CONTRACT_ID = "video.source.catalog.v1"
ARCHIVE_INTEGRATION_ID = "media.video.archive.v1"
VAULT_CAPTURE_INTEGRATION_ID = "vault.capture.v1"
REFERENCE_PATTERN = re.compile(r"^result:([1-9]|1[0-9]|20)$")


@dataclass(frozen=True, slots=True)
class _Provider:
    module_id: str
    integration_id: str


@dataclass(slots=True)
class MikuSessionContext:
    references: list[MikuReference] | None = None


class MikuActionSigner:
    def __init__(self, secret: str | None = None, ttl_seconds: int = 120) -> None:
        self.secret = (secret or get_settings().MASTER_API_KEY).encode()
        self.ttl_seconds = ttl_seconds

    def create(self, user_id: int, reference: MikuReference) -> str:
        return self.create_payload(
            user_id,
            "archive",
            {"entity_type": reference.entity_type, "entity_id": reference.item_id},
        )

    def create_payload(self, user_id: int, action: str, data: dict[str, Any]) -> str:
        payload = {"action": action, "exp": int(time.time()) + self.ttl_seconds, "user_id": user_id, **data}
        encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).digest()
        return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    def verify(self, token: str, user_id: int) -> dict[str, Any]:
        try:
            encoded, supplied = token.split(".", 1)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or not re.fullmatch(r"[A-Za-z0-9_-]+", supplied):
                raise ValueError("Non-canonical token encoding")
            expected = hmac.new(self.secret, encoded.encode(), hashlib.sha256).digest()
            signature = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
            payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise MikuQueryError("Invalid action confirmation") from exc
        if not isinstance(payload, dict):
            raise MikuQueryError("Invalid action confirmation")
        canonical_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        if not hmac.compare_digest(supplied, canonical_signature) or not hmac.compare_digest(
            signature, expected
        ):
            raise MikuQueryError("Invalid action confirmation")
        if payload.get("user_id") != user_id or payload.get("exp", 0) < int(time.time()):
            raise MikuQueryError("Action confirmation expired")
        action = payload.get("action")
        if action == "invoke":
            integration_id = payload.get("integration_id")
            parameters = payload.get("parameters")
            if (
                not isinstance(integration_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_.-]*\.v[1-9][0-9]*", integration_id)
                or not isinstance(parameters, dict)
                or len(parameters) > 20
            ):
                raise MikuQueryError("Action is not allowed")
            return payload
        if action == "archive" and payload.get("entity_type") == "youtube_video":
            return payload
        if action in {"note", "bookmark"}:
            try:
                VaultCaptureRequest.model_validate(
                    {
                        "kind": action,
                        "title": payload.get("title"),
                        "content": payload.get("content"),
                        "url": payload.get("url"),
                    }
                )
            except ValidationError as exc:
                raise MikuQueryError("Action is not allowed") from exc
            return payload
        raise MikuQueryError("Action is not allowed")


action_signer = MikuActionSigner()


def audit_turn(db: AsyncSession, user, request_id: str, transport: str, reply: MikuReply) -> None:
    db.add(
        MikuTurnAudit(
            user_id=user.id,
            request_id=request_id,
            transport=transport,
            command=reply.command,
            result_count=len(reply.references),
            warning_count=len(reply.warnings),
        )
    )


class _Registry(Protocol):
    def integration_catalog(self, consumer_id: str | None = None) -> list[dict[str, Any]]: ...

    async def invoke_integration(
        self,
        integration_id: str,
        payload: dict[str, Any],
        context: IntegrationContext,
    ) -> dict[str, Any]: ...

    async def resolve_integration_resource(
        self,
        integration_id: str,
        payload: dict[str, Any],
        context: IntegrationContext,
    ) -> IntegrationResource: ...

    def storage_owner(self, namespace: str) -> str | None: ...

    def validate_integration_request(
        self,
        integration_id: str,
        payload: dict[str, Any],
        context: IntegrationContext,
    ) -> dict[str, Any]: ...


class _Planner(Protocol):
    async def decide(
        self,
        message: str,
        tools: list[MikuToolDefinition] | None = None,
        context: list[MikuContextReference] | None = None,
    ) -> MikuDecision: ...


class _TokenStore(Protocol):
    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> Any: ...


def _providers(registry: _Registry) -> list[_Provider]:
    catalog = registry.integration_catalog(consumer_id="miku")
    return [
        _Provider(module_id=item["module_id"], integration_id=item["id"])
        for item in catalog
        if item["contract"] == CONTRACT_ID and item["effects"]["effect"] == "read"
    ]


def _source_providers(registry: _Registry) -> list[_Provider]:
    return [
        _Provider(module_id=item["module_id"], integration_id=item["id"])
        for item in registry.integration_catalog(consumer_id="miku")
        if item["contract"] == SOURCE_CONTRACT_ID and item["effects"]["effect"] == "read"
    ]


def capabilities(registry: _Registry) -> MikuCapabilities:
    return MikuCapabilities(
        commands=list(COMMANDS),
        providers=[
            *(
                MikuProvider(module_id=provider.module_id, integration_id=provider.integration_id)
                for provider in _providers(registry)
            ),
            *(
                MikuProvider(
                    module_id=provider.module_id,
                    integration_id=provider.integration_id,
                    contract=SOURCE_CONTRACT_ID,
                )
                for provider in _source_providers(registry)
            ),
        ],
    )


def runtime_tools(registry: _Registry) -> list[MikuToolDefinition]:
    tools = []
    for item in registry.integration_catalog(consumer_id="miku"):
        schema = item["request_schema"]
        compact_schema = {
            "type": schema.get("type", "object"),
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
        }
        contract = item["contract"]
        tools.append(
            MikuToolDefinition(
                integration_id=item["id"],
                module_id=item["module_id"],
                contract=contract,
                effect=item["effects"]["effect"],
                description=item.get("description", "")
                or (
                    f"{item['effects']['effect']} API provided by module {item['module_id']}"
                    + (f" implementing {contract}" if contract else "")
                ),
                input_schema=compact_schema,
            )
        )
    return tools


def _runtime_context(context: MikuSessionContext | None) -> list[MikuContextReference]:
    if not context or not context.references:
        return []
    return [
        MikuContextReference(
            ref=item.ref,
            module_id=item.module_id,
            item_id=item.item_id,
            entity_type=item.entity_type,
            kind=item.kind,
            title=item.title,
            playable=item.playable,
            readable=item.readable,
        )
        for item in context.references
    ]


def _trim(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _reference(module_id: str, item, index: int) -> MikuReference:
    resource_url = None
    open_url = None
    if module_id == "alllib":
        open_url = f"/alllib/reader/{quote(str(item.id), safe='')}"
    elif item.playable or item.readable:
        resource_url = f"/api/miku/resources/{quote(module_id, safe='')}/{quote(str(item.id), safe='')}"
        open_url = resource_url
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
        entity_type=item.kind,
        open_url=open_url,
        resource_url=resource_url,
    )


def _source_reference(module_id: str, item, index: int) -> MikuReference:
    open_url = f"/youtube/watch/{quote(item.entity_id, safe='')}" if item.kind == "video" else None
    return MikuReference(
        ref=f"result:{index}",
        module_id=module_id,
        item_id=item.entity_id,
        kind=item.kind,
        title=_trim(item.title, 160) or "Untitled",
        subtitle=_trim(item.channel_title, 160),
        summary=_trim(item.description, 300),
        playable=item.kind == "video",
        entity_type=item.entity_type,
        open_url=open_url,
    )


def _context_reference(context: MikuSessionContext | None, value: str) -> MikuReference:
    match = REFERENCE_PATTERN.fullmatch(value.casefold())
    if not match or not context or not context.references:
        raise MikuQueryError("Use a result reference from the current session, for example result:1.")
    index = int(match.group(1)) - 1
    if index >= len(context.references):
        raise MikuQueryError("Result reference is unavailable in the current session.")
    return context.references[index]


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
    runtime: _Planner = miku_runtime_client,
    context: MikuSessionContext | None = None,
) -> MikuReply:
    decision = await runtime.decide(request.message, runtime_tools(registry), _runtime_context(context))
    command, argument = decision.command, decision.argument
    providers = _providers(registry)

    if command == "invoke":
        catalog_item = next(
            (
                item
                for item in registry.integration_catalog(consumer_id="miku")
                if item["id"] == decision.integration_id
            ),
            None,
        )
        if not catalog_item:
            raise MikuQueryError("The selected API is unavailable.")
        integration_context = IntegrationContext(
            session=db,
            user=user,
            registry=registry,
            consumer_id="miku",
        )
        try:
            parameters = registry.validate_integration_request(
                decision.integration_id or "",
                decision.parameters,
                integration_context,
            )
        except (
            IntegrationNotFoundError,
            IntegrationRejectedError,
            IntegrationUnavailableError,
            ValidationError,
        ):
            raise MikuQueryError("The selected API arguments are invalid.")
        effect = catalog_item["effects"]["effect"]
        if effect == "create":
            entity_type = parameters.get("entity_type")
            entity_id = parameters.get("entity_id")
            if entity_type or entity_id:
                current_references = context.references if context and context.references else []
                if not any(
                    item.entity_type == entity_type and item.item_id == str(entity_id)
                    for item in current_references
                ):
                    raise MikuQueryError("The action must target a result from the current session.")
            return MikuReply(
                command=("archive" if decision.integration_id == ARCHIVE_INTEGRATION_ID else "note"),
                text="Confirmation is required before invoking this API.",
                pending_action=MikuPendingAction(
                    action="invoke",
                    label=f"Run {decision.integration_id}",
                    summary=f"Confirm create action in {catalog_item['module_id']}",
                    confirmation_token=action_signer.create_payload(
                        user.id,
                        "invoke",
                        {"integration_id": decision.integration_id, "parameters": parameters},
                    ),
                ),
            )
        if effect != "read":
            raise MikuQueryError("The selected API effect is not allowed.")
        try:
            result = await registry.invoke_integration(
                decision.integration_id or "",
                parameters,
                integration_context,
            )
            result_limit = min(request.limit, int(parameters.get("limit", request.limit)))
            if catalog_item["contract"] == CONTRACT_ID:
                library = LibraryResult.model_validate(result)
                if library.module_id != catalog_item["module_id"]:
                    raise IntegrationRejectedError("Provider returned the wrong module ID")
                items = library.items if library.items else ([library.item] if library.item else [])
                references = [
                    _reference(catalog_item["module_id"], item, index)
                    for index, item in enumerate(items[:result_limit], 1)
                ]
                reply_command = "list"
            elif catalog_item["contract"] == SOURCE_CONTRACT_ID:
                source = VideoSourceResult.model_validate(result)
                references = [
                    _source_reference(catalog_item["module_id"], item, index)
                    for index, item in enumerate(source.items[:result_limit], 1)
                ]
                reply_command = "discover"
            else:
                raise MikuQueryError("The selected read API has no assistant renderer.")
        except (
            IntegrationNotFoundError,
            IntegrationRejectedError,
            IntegrationServiceError,
            IntegrationUnavailableError,
            ValidationError,
        ) as exc:
            raise MikuQueryError("The selected API could not complete the request.") from exc
        if context is not None:
            context.references = references
        return MikuReply(
            command=reply_command,
            text=f"Returned {len(references)} item(s) from {catalog_item['module_id']}.",
            references=references,
        )

    if command == "help":
        return MikuReply(
            command=command,
            text="Commands: help, sources, list, find, discover, open, play, archive, note, bookmark, repeat.",
        )
    if command == "respond":
        return MikuReply(command=command, text=_trim(argument, 500) or "I cannot answer that request.")
    if command == "sources":
        names = sorted({provider.module_id for provider in [*providers, *_source_providers(registry)]})
        return MikuReply(command=command, text=f"Available sources: {', '.join(names) or 'none'}.")
    if command == "repeat":
        if not context or not context.references:
            raise MikuQueryError("There is no previous result to repeat.")
        return MikuReply(
            command=command,
            text=f"Repeating {len(context.references)} previous item(s).",
            references=context.references,
        )
    if command in {"open", "play"}:
        reference = _context_reference(context, argument)
        target = reference.resource_url if command == "play" else reference.open_url
        if not target or (command == "play" and not reference.playable):
            raise MikuQueryError(f"{reference.ref} cannot be {command}ed.")
        return MikuReply(
            command=command,
            text=f"{command.title()}: {reference.title}",
            references=[reference],
            client_action=cast(Literal["open", "play"], command),
        )
    if command == "archive":
        reference = _context_reference(context, argument)
        if reference.entity_type != "youtube_video":
            raise MikuQueryError("Only discovered YouTube videos can be archived.")
        available = {
            item["id"]
            for item in registry.integration_catalog(consumer_id="miku")
            if item["effects"]["effect"] == "create"
        }
        if ARCHIVE_INTEGRATION_ID not in available:
            raise MikuQueryError("Video Archive is unavailable.")
        if decision.integration_id and decision.integration_id != ARCHIVE_INTEGRATION_ID:
            raise MikuQueryError("The selected API cannot archive a video.")
        return MikuReply(
            command=command,
            text="Confirmation is required before starting an archive job.",
            references=[reference],
            pending_action=MikuPendingAction(
                action="archive",
                label="Archive video",
                summary=f"Archive {reference.title} at 720p",
                confirmation_token=action_signer.create(user.id, reference),
            ),
        )
    if command in {"note", "bookmark"}:
        available = {
            item["id"]
            for item in registry.integration_catalog(consumer_id="miku")
            if item["effects"]["effect"] == "create"
        }
        if VAULT_CAPTURE_INTEGRATION_ID not in available:
            raise MikuQueryError("Vault capture is unavailable.")
        if decision.integration_id and decision.integration_id != VAULT_CAPTURE_INTEGRATION_ID:
            raise MikuQueryError("The selected API cannot save to Vault.")
        try:
            capture = VaultCaptureRequest.model_validate(
                {
                    "kind": command,
                    "title": _trim(argument, 80) or command.title(),
                    "content": argument if command == "note" else None,
                    "url": argument if command == "bookmark" else None,
                }
            )
        except ValidationError as exc:
            raise MikuQueryError(f"Invalid {command} content.") from exc
        data = capture.model_dump(mode="json")
        return MikuReply(
            command=command,
            text="Confirmation is required before saving to Vault.",
            pending_action=MikuPendingAction(
                action=cast(Literal["note", "bookmark"], command),
                label=f"Save {command}",
                summary=f"Save {capture.title} to Vault",
                confirmation_token=action_signer.create_payload(user.id, command, data),
            ),
        )

    if command == "discover":
        providers = _source_providers(registry)
        if decision.integration_id:
            providers = [
                provider for provider in providers if provider.integration_id == decision.integration_id
            ]
        if not providers:
            raise MikuQueryError("No video discovery source is available.")
        references: list[MikuReference] = []
        warnings: list[str] = []
        for provider in providers:
            try:
                result = await registry.invoke_integration(
                    provider.integration_id,
                    {"operation": "search", "query": argument},
                    IntegrationContext(session=db, user=user, registry=registry, consumer_id="miku"),
                )
                source = VideoSourceResult.model_validate(result)
            except (
                IntegrationNotFoundError,
                IntegrationRejectedError,
                IntegrationServiceError,
                IntegrationUnavailableError,
                ValidationError,
            ):
                warnings.append(f"{provider.module_id} is unavailable")
                continue
            start_index = len(references) + 1
            references.extend(
                _source_reference(provider.module_id, item, start_index + index)
                for index, item in enumerate(source.items[: request.limit - len(references)])
            )
            if len(references) >= request.limit:
                break
        reply = MikuReply(
            command=command,
            text=f"Discovered {len(references)} item(s).",
            references=references,
            warnings=warnings,
        )
        if context is not None:
            context.references = reply.references
        return reply

    selected = providers
    if decision.integration_id:
        selected = [provider for provider in providers if provider.integration_id == decision.integration_id]
        if not selected:
            raise MikuQueryError("The selected library API is unavailable.")
    elif command == "list" and argument:
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
    reply = MikuReply(
        command=command,
        text=f"{action} {len(references)} item(s).",
        references=references,
        warnings=warnings,
    )
    if context is not None:
        context.references = references
    return reply


async def confirm_action(
    token: str,
    db: AsyncSession,
    user,
    registry: _Registry,
    token_store: _TokenStore,
    signer: MikuActionSigner = action_signer,
) -> MikuActionResult:
    payload = signer.verify(token, user.id)
    token_digest = hashlib.sha256(token.encode()).hexdigest()
    if not await token_store.set(f"miku:action:{token_digest}", "1", ex=180, nx=True):
        raise MikuQueryError("Action confirmation was already used")
    action = payload["action"]
    if action == "invoke":
        integration_id = payload["integration_id"]
        catalog_item = next(
            (
                item
                for item in registry.integration_catalog(consumer_id="miku")
                if item["id"] == integration_id and item["effects"]["effect"] == "create"
            ),
            None,
        )
        if not catalog_item:
            raise MikuQueryError("The confirmed API is unavailable")
        request = registry.validate_integration_request(
            integration_id,
            payload["parameters"],
            IntegrationContext(session=db, user=user, registry=registry, consumer_id="miku"),
        )
    elif action == "archive":
        integration_id = ARCHIVE_INTEGRATION_ID
        request = {
            "entity_type": payload["entity_type"],
            "entity_id": payload["entity_id"],
            "quality": "720",
        }
    else:
        integration_id = VAULT_CAPTURE_INTEGRATION_ID
        request = {
            "kind": action,
            "title": payload["title"],
            "content": payload.get("content"),
            "url": payload.get("url"),
        }
    result = await registry.invoke_integration(
        integration_id,
        request,
        IntegrationContext(session=db, user=user, registry=registry, consumer_id="miku"),
    )
    return MikuActionResult(
        status=result["status"],
        message=_trim(result.get("message"), 300) or "Action dispatched",
        task_id=str(result["task_id"]) if result.get("task_id") else None,
    )


async def job_status(task_id: str) -> MikuJobStatus | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        raise MikuQueryError("Invalid task ID")
    task = next((item for item in await tracked_tasks() if item.get("task_id") == task_id), None)
    if not task:
        return None
    return MikuJobStatus(
        task_id=task_id,
        module_id=str(task.get("module", "unknown"))[:63],
        status=_trim(task.get("status"), 120) or "running",
        progress=_trim(task.get("progress"), 32),
        title=_trim(task.get("title"), 160),
    )


async def resolve_resource(
    module_id: str,
    item_id: str,
    child_id: str | None,
    page: int | None,
    db: AsyncSession,
    user,
    registry: _Registry,
) -> IntegrationResource:
    provider = next((item for item in _providers(registry) if item.module_id == module_id), None)
    if not provider:
        raise MikuQueryError("Unknown or unavailable resource provider")
    resource = await registry.resolve_integration_resource(
        provider.integration_id,
        {"item_id": item_id, "child_id": child_id, "page": page},
        IntegrationContext(session=db, user=user, registry=registry, consumer_id="miku"),
    )
    if resource.storage_path:
        parts = resource.storage_path.split("/")
        if (
            resource.storage_path.startswith("/")
            or "\\" in resource.storage_path
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise IntegrationRejectedError("Provider returned an invalid storage resource")
        namespace = parts[0]
        if registry.storage_owner(namespace) != module_id:
            raise IntegrationRejectedError("Provider returned a foreign storage resource")
    return resource
