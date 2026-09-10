import asyncio
import os
from dataclasses import asdict
from typing import Any

import httpx

from app.core.browser_snapshots import browser_snapshot_store
from app.core.config import get_settings
from app.core.module_types import BrowserPolicySpec, browser_policy_fingerprint
from app.core.modules import module_registry


class BrowserRuntimeClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("BROWSER_RUNTIME_URL", "http://browser-runtime:8765")).rstrip(
            "/"
        )
        self._policy_locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, tuple[str, str, str]] = {}

    def _policy_lock(self, policy_id: str) -> asyncio.Lock:
        return self._policy_locks.setdefault(policy_id, asyncio.Lock())

    def _validate_session_policy(
        self,
        session_id: str,
        module_id: str,
        policy_id: str,
        fingerprint: str,
    ) -> None:
        resolved = module_registry.browser_policy(policy_id)
        if (
            not resolved
            or resolved[0].id != module_id
            or browser_policy_fingerprint(resolved[1]) != fingerprint
        ):
            self._sessions.pop(session_id, None)
            raise ValueError("Browser session policy is no longer active")
        self._sessions[session_id] = (module_id, policy_id, fingerprint)

    async def _ensure_active(self, session_id: str) -> None:
        known = self._sessions.get(session_id)
        try:
            if known:
                self._validate_session_policy(session_id, known[0], known[1], known[2])
                return
            status = (await self._request("GET", f"/sessions/{session_id}")).json()
            self._validate_session_policy(
                session_id,
                status["module_id"],
                status["policy_id"],
                status["policy_fingerprint"],
            )
        except ValueError:
            try:
                await self._request("DELETE", f"/sessions/{session_id}")
            except Exception:
                pass
            raise

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        timeout: float = 70,
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as client:
                response = await client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise RuntimeError("Browser runtime is unavailable") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            if response.status_code == 404:
                raise LookupError(detail or "Browser session was not found")
            if response.status_code == 400:
                raise ValueError(detail or "Browser request was rejected")
            raise RuntimeError(detail or "Browser runtime request failed")
        return response

    async def start(
        self,
        module_id: str,
        policy: BrowserPolicySpec,
        *,
        locale: str = "en-US",
        restore_snapshot: bool = True,
        mode: str = "interactive",
    ) -> dict[str, Any]:
        if not get_settings().BROWSER_RUNTIME_ENABLED:
            raise RuntimeError("Browser runtime is disabled")
        async with self._policy_lock(policy.id):
            configured_hosts = {
                host.strip().lower().rstrip(".")
                for host in get_settings().BROWSER_EGRESS_HOSTS.split(",")
                if host.strip()
            }
            if not all(
                any(host == allowed or host.endswith(f".{allowed}") for allowed in configured_hosts)
                for host in policy.allowed_hosts
            ):
                raise ValueError("Browser policy requires hosts outside the deployment egress list")
            storage_state = (
                await browser_snapshot_store.load(policy.id, module_id, policy)
                if restore_snapshot and policy.persist_snapshot
                else None
            )
            response = await self._request(
                "POST",
                "/sessions",
                json={
                    "module_id": module_id,
                    "policy": asdict(policy),
                    "locale": locale,
                    "restore_snapshot": restore_snapshot,
                    "mode": mode,
                    "storage_state": storage_state,
                },
            )
        payload = response.json()
        self._validate_session_policy(
            payload["session_id"], module_id, policy.id, payload["policy_fingerprint"]
        )
        return payload

    async def status(self, session_id: str) -> dict[str, Any]:
        payload = (await self._request("GET", f"/sessions/{session_id}")).json()
        try:
            self._validate_session_policy(
                session_id,
                payload["module_id"],
                payload["policy_id"],
                payload["policy_fingerprint"],
            )
        except ValueError:
            try:
                await self._request("DELETE", f"/sessions/{session_id}")
            except Exception:
                pass
            raise
        return payload

    async def screenshot(self, session_id: str) -> bytes:
        await self._ensure_active(session_id)
        return (await self._request("GET", f"/sessions/{session_id}/frame")).content

    async def click(self, session_id: str, x: float, y: float) -> None:
        await self._ensure_active(session_id)
        await self._request("POST", f"/sessions/{session_id}/click", json={"x": x, "y": y})

    async def type_text(self, session_id: str, text: str) -> None:
        await self._ensure_active(session_id)
        await self._request("POST", f"/sessions/{session_id}/type", json={"text": text})

    async def press(self, session_id: str, key: str) -> None:
        await self._ensure_active(session_id)
        await self._request("POST", f"/sessions/{session_id}/key", json={"key": key})

    async def scroll(self, session_id: str, delta_y: float) -> None:
        await self._ensure_active(session_id)
        await self._request("POST", f"/sessions/{session_id}/scroll", json={"delta_y": delta_y})

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        await self._ensure_active(session_id)
        return (await self._request("POST", f"/sessions/{session_id}/navigate", json={"url": url})).json()

    async def query(self, session_id: str, selector: str, *, limit: int = 20) -> list[dict]:
        await self._ensure_active(session_id)
        response = await self._request(
            "POST",
            f"/sessions/{session_id}/query",
            json={"selector": selector, "limit": limit},
        )
        return response.json()["items"]

    async def save_snapshot(self, session_id: str) -> dict:
        status = await self.status(session_id)
        resolved = module_registry.browser_policy(status["policy_id"])
        if not resolved or resolved[0].id != status["module_id"]:
            raise ValueError("Browser session policy is no longer active")
        record, policy = resolved
        async with self._policy_lock(policy.id):
            storage_state = (await self._request("GET", f"/sessions/{session_id}/storage-state")).json()
            await browser_snapshot_store.save(policy.id, record.id, storage_state, policy)
        return {"policy_id": policy.id, "saved": True}

    async def close(self, session_id: str) -> None:
        try:
            await self._request("DELETE", f"/sessions/{session_id}")
        finally:
            self._sessions.pop(session_id, None)

    async def close_policy(self, policy_id: str) -> None:
        await self._request("DELETE", f"/policies/{policy_id}/sessions")
        self._sessions = {
            session_id: metadata
            for session_id, metadata in self._sessions.items()
            if metadata[1] != policy_id
        }

    async def delete_snapshot(self, policy_id: str) -> None:
        async with self._policy_lock(policy_id):
            if get_settings().BROWSER_RUNTIME_ENABLED:
                await self.close_policy(policy_id)
            await browser_snapshot_store.delete(policy_id)


browser_runtime_client = BrowserRuntimeClient()


async def revoke_browser_credentials(credential_scope: str) -> None:
    resolved = module_registry.browser_policy_for_credential_scope(credential_scope)
    if not resolved:
        return
    _, policy = resolved
    await browser_runtime_client.delete_snapshot(policy.id)
