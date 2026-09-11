import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.module_types import BrowserPolicySpec
from app.core.secret_values import decrypt_secret_value, encrypt_secret_value

SNAPSHOT_VERSION = 1
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024


class BrowserSnapshotStore:
    def __init__(self, root: Path | None = None) -> None:
        configured_root = Path(get_settings().LOCAL_STORAGE_ROOT)
        self.root = root or configured_root / "config" / "browser-snapshots"

    def _path(self, policy_id: str) -> Path:
        return self.root / f"{policy_id}.snapshot"

    def _legacy_path(self, policy_id: str) -> Path:
        return self.root / f"{policy_id}.json.enc"

    def _save_sync(
        self,
        policy_id: str,
        module_id: str,
        storage_state: dict,
        policy: BrowserPolicySpec | None = None,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        payload = json.dumps(
            {
                "version": SNAPSHOT_VERSION,
                "policy_id": policy_id,
                "module_id": module_id,
                "updated_at": int(time.time()),
                "storage_state": self._filter_state(storage_state, policy) if policy else storage_state,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Browser snapshot exceeds the 10 MiB limit")
        target = self._path(policy_id)
        temporary = target.with_suffix(f"{target.suffix}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(encrypt_secret_value(payload), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)

    async def save(
        self,
        policy_id: str,
        module_id: str,
        storage_state: dict,
        policy: BrowserPolicySpec | None = None,
    ) -> None:
        await asyncio.to_thread(self._save_sync, policy_id, module_id, storage_state, policy)

    @staticmethod
    def _filter_state(storage_state: dict, policy: BrowserPolicySpec) -> dict:
        now = int(time.time())

        def allowed_host(host: str) -> bool:
            normalized = host.lower().lstrip(".").rstrip(".")
            return any(
                normalized == allowed or normalized.endswith(f".{allowed}")
                for allowed in policy.allowed_hosts
            )

        cookies = [
            cookie
            for cookie in storage_state.get("cookies") or []
            if allowed_host(str(cookie.get("domain") or ""))
            and cookie.get("name") in policy.persisted_cookie_names
            and (int(cookie.get("expires") or 0) <= 0 or int(cookie.get("expires") or 0) > now)
        ]
        origins = [
            origin
            for origin in storage_state.get("origins") or []
            if allowed_host(urlparse(str(origin.get("origin") or "")).hostname or "")
            and origin.get("origin") in policy.persisted_origins
        ]
        return {"cookies": cookies, "origins": origins}

    def _load_sync(
        self,
        policy_id: str,
        module_id: str | None = None,
        policy: BrowserPolicySpec | None = None,
    ) -> dict | None:
        path = self._path(policy_id)
        legacy = False
        if not path.is_file() and self._legacy_path(policy_id).is_file():
            path = self._legacy_path(policy_id)
            legacy = True
        if not path.is_file():
            return None
        payload = json.loads(decrypt_secret_value(path.read_text(encoding="utf-8")))
        if payload.get("version") != SNAPSHOT_VERSION or payload.get("policy_id") != policy_id:
            raise ValueError("Browser snapshot has an incompatible format")
        if module_id and payload.get("module_id") != module_id:
            raise ValueError("Browser snapshot belongs to another module")
        storage_state = payload.get("storage_state")
        if not isinstance(storage_state, dict):
            raise ValueError("Browser snapshot has no storage state")
        if legacy:
            os.replace(path, self._path(policy_id))
        return self._filter_state(storage_state, policy) if policy else storage_state

    async def load(
        self,
        policy_id: str,
        module_id: str | None = None,
        policy: BrowserPolicySpec | None = None,
    ) -> dict | None:
        return await asyncio.to_thread(self._load_sync, policy_id, module_id, policy)

    async def delete(self, policy_id: str) -> None:
        await asyncio.to_thread(self._path(policy_id).unlink, missing_ok=True)
        await asyncio.to_thread(self._legacy_path(policy_id).unlink, missing_ok=True)

    async def exists(self, policy_id: str) -> bool:
        return await asyncio.to_thread(
            lambda: self._path(policy_id).is_file() or self._legacy_path(policy_id).is_file()
        )

    @staticmethod
    def _cookies_netscape(storage_state: dict | None) -> str | None:
        if not storage_state:
            return None
        now = int(time.time())
        cookies = [
            cookie
            for cookie in storage_state.get("cookies") or []
            if int(cookie.get("expires") or 0) <= 0 or int(cookie.get("expires") or 0) > now
        ]
        if not cookies:
            return None
        lines = ["# Netscape HTTP Cookie File", "# Generated by NetSanctum Browser Runtime"]
        for cookie in cookies:
            domain = str(cookie.get("domain") or "")
            lines.append(
                "\t".join(
                    (
                        domain,
                        "TRUE" if domain.startswith(".") else "FALSE",
                        str(cookie.get("path") or "/"),
                        "TRUE" if cookie.get("secure") else "FALSE",
                        str(max(int(cookie.get("expires") or 0), 0)),
                        str(cookie.get("name") or ""),
                        str(cookie.get("value") or ""),
                    )
                )
            )
        return "\n".join(lines) + "\n"

    def cookies_netscape_sync(self, policy_id: str) -> str | None:
        return self._cookies_netscape(self._load_sync(policy_id))

    async def cookies_netscape(self, policy_id: str) -> str | None:
        return await asyncio.to_thread(self.cookies_netscape_sync, policy_id)

    def cookies_for_scope_sync(self, credential_scope: str) -> str | None:
        from app.core.modules import module_registry

        resolved = module_registry.browser_policy_for_credential_scope(credential_scope)
        if not resolved:
            return None
        record, policy = resolved
        storage_state = self._load_sync(policy.id, record.id, policy)
        if not storage_state:
            return None
        present_names = {cookie.get("name") for cookie in storage_state.get("cookies") or []}
        if policy.required_cookie_names and not present_names & set(policy.required_cookie_names):
            return None
        return self._cookies_netscape(storage_state)

    async def cookies_for_scope(self, credential_scope: str) -> str | None:
        return await asyncio.to_thread(self.cookies_for_scope_sync, credential_scope)


browser_snapshot_store = BrowserSnapshotStore()
