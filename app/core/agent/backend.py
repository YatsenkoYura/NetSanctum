"""The application side of the world, as seen from the isolated agent runtime."""

import asyncio
from typing import Any

import httpx

from app.core.agent.catalog import AgentTool
from app.core.agent.fetch import RemoteFetchError
from app.core.agent.primitives import AgentFetchResult

CATALOG_TIMEOUT_SECONDS = 15
INVOKE_TIMEOUT_SECONDS = 60
RESOURCE_TIMEOUT_SECONDS = 45
FETCH_TIMEOUT_SECONDS = 35


class AgentBackendUnavailableError(RuntimeError):
    """The application could not answer a capability request."""


MAX_REMOTE_DESCRIPTION = 300


def _tolerant(tool: dict[str, Any]) -> AgentTool:
    """The catalog crosses a process boundary: never fail a turn over a long sentence."""
    payload = dict(tool)
    description = " ".join(str(payload.get("description") or "").split())
    if len(description) > MAX_REMOTE_DESCRIPTION:
        description = f"{description[:MAX_REMOTE_DESCRIPTION].rsplit(' ', 1)[0]}."
    payload["description"] = description or "Tool."
    return AgentTool.model_validate(payload)


class HttpToolBackend:
    """Talks to web over the internal contract; owns no state and no database."""

    def __init__(
        self,
        base_url: str,
        internal_key: str,
        *,
        consumer_id: str = "miku",
        transport: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.internal_key = internal_key
        self.consumer_id = consumer_id
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        return {"X-Agent-Key": self.internal_key, "Content-Type": "application/json"}

    async def catalog(self) -> list[AgentTool]:
        try:
            async with httpx.AsyncClient(timeout=CATALOG_TIMEOUT_SECONDS, transport=self.transport) as client:
                response = await client.get(
                    f"{self.base_url}/internal/agent/catalog",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise AgentBackendUnavailableError("catalog unavailable") from exc
        if response.status_code != 200:
            raise AgentBackendUnavailableError("catalog unavailable")
        payload = response.json()
        return [_tolerant(tool) for tool in payload.get("tools", [])]

    async def invoke(self, integration_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=INVOKE_TIMEOUT_SECONDS, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/internal/agent/invoke",
                    headers=self._headers(),
                    json={
                        "integration_id": integration_id,
                        "parameters": parameters,
                        "consumer_id": self.consumer_id,
                    },
                )
        except httpx.HTTPError as exc:
            raise AgentBackendUnavailableError("invoke unavailable") from exc
        if response.status_code == 404:
            raise LookupError("this capability is unavailable")
        if response.status_code == 422:
            raise ValueError("the request was rejected")
        if response.status_code != 200:
            raise AgentBackendUnavailableError(f"invoke failed ({response.status_code})")
        return response.json().get("result", {})

    async def read(self, module_id: str, item_id: str, max_chars: int) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=RESOURCE_TIMEOUT_SECONDS, transport=self.transport
            ) as client:
                response = await client.post(
                    f"{self.base_url}/internal/agent/resource",
                    headers=self._headers(),
                    json={
                        "module_id": module_id,
                        "item_id": item_id,
                        "ref": "result:1",
                        "max_chars": max_chars,
                        "consumer_id": self.consumer_id,
                    },
                )
        except httpx.HTTPError as exc:
            raise AgentBackendUnavailableError("resource unavailable") from exc
        if response.status_code == 404:
            raise LookupError("this item has no stored content")
        if response.status_code == 422:
            raise ValueError("the content request was rejected")
        if response.status_code != 200:
            raise AgentBackendUnavailableError(f"resource read failed ({response.status_code})")
        return response.json()

    async def fetch(self, url: str, max_chars: int) -> AgentFetchResult:
        """Fetching happens here, in the sidecar, where the egress network lives."""
        from app.core.agent.fetch import fetch_public_text

        try:
            return await asyncio.wait_for(
                fetch_public_text(url, max_chars=max_chars), timeout=FETCH_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise TimeoutError("fetch took too long") from exc
        except RemoteFetchError:
            raise


__all__ = ["AgentBackendUnavailableError", "HttpToolBackend"]
