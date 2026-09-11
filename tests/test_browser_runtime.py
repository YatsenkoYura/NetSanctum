import asyncio
import tempfile
import tomllib
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import yaml

from app.core import browser_egress_proxy
from app.core.browser_snapshots import BrowserSnapshotStore
from app.core.browser_worker import WorkerPolicy
from app.core.module_types import BrowserPolicySpec
from app.core.modules import ModuleRegistry


class BrowserRuntimeContractTests(unittest.TestCase):
    def test_browser_sidecar_has_no_secrets_storage_or_public_port(self):
        compose = yaml.safe_load(Path("docker-compose.yml").read_text())
        browser = compose["services"]["browser-runtime"]

        self.assertEqual(["browser"], browser["profiles"])
        self.assertNotIn("env_file", browser)
        self.assertNotIn("volumes", browser)
        self.assertNotIn("ports", browser)
        self.assertEqual({"browser-control", "browser-proxy"}, set(browser["networks"]))
        self.assertTrue(browser["read_only"])
        self.assertEqual(["ALL"], browser["cap_drop"])
        self.assertNotIn("cap_add", browser)
        self.assertEqual(["seccomp=./docker/browser-seccomp.json"], browser["security_opt"])
        proxy = compose["services"]["browser-proxy"]
        self.assertEqual(["browser"], proxy["profiles"])
        self.assertNotIn("env_file", proxy)
        self.assertNotIn("volumes", proxy)
        self.assertEqual({"browser-proxy", "browser-egress"}, set(proxy["networks"]))
        self.assertEqual(
            {"default", "backend", "media-control", "browser-control"},
            set(compose["services"]["web"]["networks"]),
        )
        self.assertTrue(compose["networks"]["browser-control"]["internal"])
        self.assertTrue(compose["networks"]["browser-proxy"]["internal"])
        self.assertEqual("1", browser["build"]["args"]["INSTALL_BROWSER_RUNTIME"])
        self.assertNotIn("INSTALL_BROWSER_RUNTIME", compose["services"]["web"]["build"]["args"])
        project = tomllib.loads(Path("pyproject.toml").read_text())
        self.assertTrue(
            any(
                dependency.startswith("playwright==")
                for dependency in project["project"]["optional-dependencies"]["browser_runtime"]
            )
        )
        self.assertFalse(
            any(
                dependency.startswith("playwright==")
                for dependency in project["project"]["optional-dependencies"]["youtube"]
            )
        )
        start_script = Path("start.sh").read_text()
        self.assertIn("--no-browser-runtime", start_script)
        self.assertIn("docker compose --profile browser up", start_script)

    def test_compose_isolates_and_persists_core_services(self):
        compose = yaml.safe_load(Path("docker-compose.yml").read_text())
        services = compose["services"]

        self.assertNotIn("env_file", services["postgres"])
        self.assertEqual(["backend"], services["postgres"]["networks"])
        self.assertEqual(["backend"], services["redis"]["networks"])
        self.assertIn("redis_data:/data", services["redis"]["volumes"])
        self.assertNotIn("backend", services["youtube-pot"]["networks"])
        self.assertIn("migrate", services)
        self.assertNotIn("app.core.migrations", " ".join(services["web"]["command"]))
        self.assertNotIn("app.core.migrations", " ".join(services["worker"]["command"]))

    def test_youtube_uses_shared_browser_window(self):
        template = Path("app/modules/youtube/templates/youtube_dashboard.html").read_text()
        base = Path("app/core/templates/base.html").read_text()

        self.assertIn('<netsanctum-browser id="youtube-account-browser"', template)
        self.assertNotIn("/api/youtube/browser", template)
        self.assertIn("/static/browser-runtime.js", base)

    def test_policy_requires_https_start_url_inside_allowlist(self):
        normalized = BrowserPolicySpec(
            id="example.normalized",
            start_url="https://www.example.com/login",
            allowed_hosts=("Example.COM.",),
        )
        self.assertEqual(("example.com",), normalized.allowed_hosts)
        with self.assertRaises(ValueError):
            BrowserPolicySpec(
                id="example.login",
                start_url="http://example.com/login",
                allowed_hosts=("example.com",),
            )
        with self.assertRaises(ValueError):
            BrowserPolicySpec(
                id="example.login",
                start_url="https://example.com/login",
                allowed_hosts=("example.com",),
                allowed_modes=("remote-desktop",),
            )
        with self.assertRaises(ValueError):
            BrowserPolicySpec(
                id="example.login",
                start_url="https://evil.example/login",
                allowed_hosts=("example.com",),
            )

    def test_egress_proxy_rejects_suffix_confusion(self):
        with patch.object(
            browser_egress_proxy,
            "ALLOWED_HOSTS",
            frozenset({"youtube.com"}),
        ):
            self.assertTrue(browser_egress_proxy._host_allowed("www.youtube.com"))
            self.assertFalse(browser_egress_proxy._host_allowed("youtube.com.evil.example"))

    def test_registry_exposes_active_browser_policy(self):
        registry = ModuleRegistry.discover({"youtube"})

        resolved = registry.browser_policy("youtube.account")
        assert resolved is not None
        record, policy = resolved

        self.assertEqual("youtube", record.id)
        self.assertEqual("youtube", policy.credential_scope)
        self.assertTrue(policy.persist_snapshot)
        self.assertEqual(policy, WorkerPolicy.model_validate(asdict(policy)).to_spec())

    def test_encrypted_snapshot_round_trip_survives_new_store_instance(self):
        state = {
            "cookies": [
                {
                    "domain": ".youtube.com",
                    "path": "/",
                    "secure": True,
                    "expires": 2000000000,
                    "name": "SAPISID",
                    "value": "secret-cookie",
                }
            ],
            "origins": [
                {
                    "origin": "https://www.youtube.com",
                    "localStorage": [{"name": "theme", "value": "dark"}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            writer = BrowserSnapshotStore(root)
            asyncio.run(writer.save("youtube.account", "youtube", state))

            raw = (root / "youtube.account.snapshot").read_text()
            self.assertNotIn("secret-cookie", raw)
            reader = BrowserSnapshotStore(root)
            self.assertEqual(state, asyncio.run(reader.load("youtube.account")))
            policy = BrowserPolicySpec(
                id="youtube.account",
                start_url="https://www.youtube.com/",
                allowed_hosts=("youtube.com",),
                persist_snapshot=True,
                required_cookie_names=("SAPISID",),
                persisted_cookie_names=("SAPISID",),
            )
            filtered = asyncio.run(reader.load("youtube.account", "youtube", policy))
            assert filtered is not None
            self.assertEqual(state["cookies"], filtered["cookies"])
            self.assertEqual([], filtered["origins"])
            with self.assertRaises(ValueError):
                asyncio.run(reader.load("youtube.account", "another_module", policy))
            cookies = asyncio.run(reader.cookies_netscape("youtube.account"))
            assert cookies is not None
            self.assertIn("SAPISID\tsecret-cookie", cookies)

            state_with_extra_credentials = {
                "cookies": [
                    *state["cookies"],
                    {
                        "domain": ".google.com",
                        "path": "/",
                        "secure": True,
                        "expires": 2000000000,
                        "name": "UNRELATED_GOOGLE_COOKIE",
                        "value": "must-not-persist",
                    },
                ],
                "origins": state["origins"],
            }
            asyncio.run(
                writer.save(
                    "youtube.account",
                    "youtube",
                    state_with_extra_credentials,
                    policy,
                )
            )
            persisted = asyncio.run(reader.load("youtube.account"))
            assert persisted is not None
            self.assertEqual(["SAPISID"], [cookie["name"] for cookie in persisted["cookies"]])

            expired = {
                "cookies": [
                    {
                        "domain": ".youtube.com",
                        "path": "/",
                        "secure": True,
                        "expires": 1,
                        "name": "SAPISID",
                        "value": "expired",
                    }
                ],
                "origins": [],
            }
            asyncio.run(writer.save("youtube.account", "youtube", expired))
            self.assertIsNone(reader.cookies_for_scope_sync("youtube"))


if __name__ == "__main__":
    unittest.main()
