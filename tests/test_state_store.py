"""The state store must not write session state to disk.

An unlock session holds a vault's data key, a download handoff holds the address
of a video the owner has not finished saving, and the throttle holds who has been
guessing at a passphrase. All three were in the same Redis as the broker, which
means the append-only log that makes the queue survive a restart also kept a data
key surviving one.

These tests pin the audit itself — what it accepts, what it refuses, and what it
does when it cannot ask — rather than the deployment, which is compose's job.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.core.state_store import audit_state_redis, require_ephemeral_state_store, state_redis_url


def fake_config(appendonly: str, save: str) -> AsyncMock:
    client = AsyncMock()
    client.config_get = AsyncMock(
        side_effect=lambda key: {"appendonly": appendonly} if key == "appendonly" else {"save": save}
    )
    client.aclose = AsyncMock()
    return client


class RedactionTests(unittest.TestCase):
    """The connection string carries a password, so it is not a thing to print.

    A single warning with the state store's credentials in it puts a live secret
    in the log file of every deployment that ever hit it, and logs get shipped
    somewhere they should not be.
    """

    SECRET = "redis://:nsredis-3f29a86c2d5b4e7791ca83f6d08e1b42@redis-state:6379/0"

    def test_the_password_is_removed(self):
        from app.core.state_store import redact

        redacted = redact(self.SECRET)

        self.assertNotIn("3f29a86c", redacted)
        self.assertIn("redis-state:6379", redacted, "the host is the useful part and is kept")

    def test_a_url_without_credentials_is_untouched(self):
        from app.core.state_store import redact

        self.assertEqual("redis://localhost:6379/0", redact("redis://localhost:6379/0"))

    def test_a_url_that_will_not_parse_is_replaced_rather_than_passed_on(self):
        """`urlsplit` is lenient about most rubbish and raises about the rest —
        a bad port, a broken bracket. A password must not survive either path."""
        from app.core.state_store import redact

        redacted = redact("redis://:hunter2@redis-state:not-a-port/0")

        self.assertNotIn("hunter2", redacted)

    def test_the_report_never_carries_the_password(self):
        from app.core.state_store import audit_state_redis

        with patch("app.core.state_store._client_for", return_value=fake_config("no", "")):
            report = asyncio.run(audit_state_redis(self.SECRET))

        self.assertNotIn("3f29a86c", str(report))

    def test_a_failure_log_does_not_carry_the_password(self):
        from app.core.state_store import audit_state_redis

        client = AsyncMock()
        client.config_get = AsyncMock(side_effect=OSError("no CONFIG here"))
        client.aclose = AsyncMock()
        with (
            patch("app.core.state_store._client_for", return_value=client),
            self.assertLogs("app.core.state_store", level="WARNING") as logs,
        ):
            asyncio.run(audit_state_redis(self.SECRET))

        self.assertNotIn("3f29a86c", "\n".join(logs.output))

    def test_the_ephemeral_check_never_raises_with_the_url_in_the_message(self):
        from app.core.state_store import require_ephemeral_state_store

        settings = type(
            "S",
            (),
            {
                "VAULT_STATE_REQUIRE_EPHEMERAL": True,
                "REDIS_URL": "redis://a",
                "VAULT_STATE_REDIS_URL": self.SECRET,
            },
        )()
        with (
            patch("app.core.state_store._client_for", return_value=fake_config("yes", "")),
            patch("app.core.state_store.get_settings", return_value=settings),
        ):
            with self.assertRaises(RuntimeError) as caught:
                asyncio.run(require_ephemeral_state_store())

        self.assertNotIn("3f29a86c", str(caught.exception))


class StateUrlTests(unittest.TestCase):
    def test_it_falls_back_to_the_general_redis(self):
        """Changing nothing has to keep working."""
        with patch("app.core.state_store.get_settings") as settings:
            settings.return_value = type(
                "S", (), {"VAULT_STATE_REDIS_URL": "", "REDIS_URL": "redis://a:6379/0"}
            )()
            self.assertEqual("redis://a:6379/0", state_redis_url())

    def test_a_configured_url_wins(self):
        with patch("app.core.state_store.get_settings") as settings:
            settings.return_value = type(
                "S", (), {"VAULT_STATE_REDIS_URL": "redis://b:6379/1", "REDIS_URL": "redis://a:6379/0"}
            )()
            self.assertEqual("redis://b:6379/1", state_redis_url())


class StateStoreAuditTests(unittest.TestCase):
    def _audit(self, appendonly: str, save: str) -> dict:
        with patch("app.core.state_store._client_for", return_value=fake_config(appendonly, save)):
            return asyncio.run(audit_state_redis("redis://state:6379/0"))

    def test_an_ephemeral_instance_reports_itself_clean(self):
        report = self._audit("no", "")

        self.assertTrue(report["reachable"])
        self.assertFalse(report["appendonly"])
        self.assertEqual("", report["save"])

    def test_appendonly_is_reported_on(self):
        self.assertTrue(self._audit("yes", "")["appendonly"])

    def test_an_unreachable_server_is_not_a_persistence_answer(self):
        client = AsyncMock()
        client.config_get = AsyncMock(side_effect=OSError("no CONFIG here"))
        client.aclose = AsyncMock()
        with patch("app.core.state_store._client_for", return_value=client):
            report = asyncio.run(audit_state_redis("redis://managed:6379/0"))

        self.assertFalse(report["reachable"])
        self.assertIsNone(report["appendonly"])

    def _require(self, appendonly: str, save: str, *, must_be_ephemeral: bool, reachable: bool = True):
        client = fake_config(appendonly, save)
        if not reachable:
            client.config_get = AsyncMock(side_effect=OSError("no CONFIG here"))
        settings = type(
            "S",
            (),
            {
                "VAULT_STATE_REQUIRE_EPHEMERAL": must_be_ephemeral,
                "REDIS_URL": "redis://a",
                "VAULT_STATE_REDIS_URL": "",
            },
        )()
        with (
            patch("app.core.state_store._client_for", return_value=client),
            patch("app.core.state_store.get_settings", return_value=settings),
        ):
            return asyncio.run(require_ephemeral_state_store())

    def test_persistence_is_refused_when_the_deployment_asks_for_it(self):
        with self.assertRaises(RuntimeError) as caught:
            self._require("yes", "", must_be_ephemeral=True)

        self.assertIn("appendonly", str(caught.exception))
        self.assertIn("VAULT_STATE_REDIS_URL", str(caught.exception))

    def test_snapshots_alone_warn_rather_than_refuse(self):
        """Snapshotting is the milder of the two and is a Redis default.

        Refusing there would break every deployment that has not read this
        sentence; a loud warning is the honest amount of force.
        """
        with self.assertLogs("app.core.state_store", level="WARNING") as logs:
            report = self._require("no", "3600 1 300", must_be_ephemeral=True)

        self.assertFalse(report["appendonly"])
        self.assertTrue(any("RDB" in line for line in logs.output), logs.output)

    def test_a_clean_instance_passes_silently(self):
        report = self._require("no", "", must_be_ephemeral=True)

        self.assertTrue(report["reachable"])

    def test_an_unreachable_server_does_not_stop_the_application(self):
        """A network problem is not a persistence problem."""
        report = self._require("no", "", must_be_ephemeral=True, reachable=False)

        self.assertFalse(report["reachable"])

    def test_the_check_is_off_by_default(self):
        """A single-Redis deployment must survive the upgrade unchanged."""
        report = self._require("yes", "3600 1", must_be_ephemeral=False)

        self.assertTrue(report["appendonly"])


class WiringTests(unittest.TestCase):
    def test_the_session_client_points_at_the_state_store(self):
        """Not a comment: the module-level client is what every call goes through."""
        import inspect

        from app.modules.vault import sealing, services

        for module in (sealing, services):
            source = inspect.getsource(module)
            client_line = next(
                line for line in source.splitlines() if line.startswith("redis_client = aioredis.Redis")
            )
            self.assertIn("state_redis_url()", client_line, f"{module.__name__} still uses the default")

    def test_startup_checks_the_store_before_anything_can_unlock(self):
        from pathlib import Path

        source = Path("app/main.py").read_text()
        check = source.index("require_ephemeral_state_store()")
        self.assertLess(check, source.index("run_startup_hooks()"))


if __name__ == "__main__":
    unittest.main()
