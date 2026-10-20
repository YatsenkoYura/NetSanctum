"""The Celery worker must never reach an async Redis client.

A `redis.asyncio` client owns a pool of connections, and each connection belongs
to the loop it was opened on. The web process has one loop for its whole life, so
an async client there is correct and is what the rest of this module uses.

A task is different. `asyncio.run(...)` builds a fresh loop and closes it the
moment it returns, so the loop the worker reaches an async client on is a
different one from the loop the previous task used. The connection the first task
opened is still in the pool, attached to a loop that no longer exists, and the
second task dies with:

    got Future attached to a different loop
    RuntimeError: Event loop is closed

There is no error at the first task and no test that fails at the second — it
looks like an intermittent download failure. So the rule is pinned here as well
as in the worker: the tasks read Redis through the synchronous client, which has
no loop to belong to.
"""

import ast
import json
import unittest
from pathlib import Path

TASKS = Path("app/modules/vault/tasks.py")
SERVICES = Path("app/modules/vault/services.py")
SEALING = Path("app/modules/vault/sealing.py")


def imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def called_functions(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.append(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.append(node.func.attr)
    return out


class TheWorkerRunsNoLoopOfItsOwnTests(unittest.TestCase):
    def setUp(self):
        self.tree = ast.parse(TASKS.read_text(encoding="utf-8"))

    def test_no_task_builds_an_event_loop(self):
        """`asyncio.run` is what closes the door between tasks. A task that needs
        to await something is asking for a loop that will not outlive it."""
        self.assertNotIn("run", called_functions(self.tree), "a Celery task must not call asyncio.run")

    def test_the_worker_does_not_import_asyncio_at_all(self):
        self.assertNotIn(
            "asyncio",
            imported_names(self.tree),
            "a module that cannot run a loop should not carry the import",
        )

    def test_no_task_awaits_a_module_level_async_client(self):
        """The whole failure in one line: an async call on a client whose pool
        belongs to some earlier loop."""
        source = TASKS.read_text(encoding="utf-8")
        for client in ("redis_client", "async_redis"):
            self.assertNotIn(f"await {client}", source, client)


class TheWorkerReadsRedisSynchronouslyTests(unittest.TestCase):
    def setUp(self):
        self.source = TASKS.read_text(encoding="utf-8")

    def test_a_synchronous_client_exists_beside_the_async_one(self):
        """Both, and named so the choice is visible at the import."""
        for source, module in (
            (SERVICES.read_text(encoding="utf-8"), "services"),
            (SEALING.read_text(encoding="utf-8"), "sealing"),
        ):
            self.assertIn("sync_redis_client = sync_redis.Redis.from_url", source, module)

    def test_the_handoff_is_taken_synchronously(self):
        """Its only caller is a task, and both handoffs carry a sealed unlock
        token that must be read through a loop-less client."""
        for name in ("take_download_handoff", "take_finalize_handoff"):
            self.assertIn(f"{name}(handoff)", self.source)
        self.assertNotIn("await take_download_handoff", self.source)
        self.assertNotIn("await take_finalize_handoff", self.source)

    def test_the_session_key_is_read_synchronously(self):
        self.assertIn("data_key_for_id_sync", self.source)

    def test_the_sync_and_async_readers_share_one_set_of_rules(self):
        """Otherwise the worker and the web can disagree about what a session
        record means, and only on the path that is hard to reach by hand.

        Three occurrences each: the definition, and one call in each reader.
        """
        sealing = SEALING.read_text(encoding="utf-8")
        for helper in ("_session_private_key", "_session_expired"):
            self.assertEqual(
                3, sealing.count(f"{helper}("), f"{helper} should back both readers, not be inlined twice"
            )

    def test_an_unreadable_record_is_not_deleted(self):
        """A record this build cannot open is another build's. Deleting it would
        close a session that is perfectly valid for whoever wrote it — and the
        two readers must agree on that, not just on the happy path."""
        sealing = SEALING.read_text(encoding="utf-8")
        reader = sealing[
            sealing.index("async def data_key_for_id(") : sealing.index("def data_key_for_id_sync(")
        ]
        self.assertIn("if private_key is None:", reader)
        self.assertIn("return None", reader)
        sync_reader = sealing[
            sealing.index("def data_key_for_id_sync(") : sealing.index("def _session_private_key(")
        ]
        self.assertIn("if private_key is None:", sync_reader)


class TwoTasksInARowTests(unittest.TestCase):
    """The shape of the original failure, which no single call would show.

    The bug needed a *second* task: the first opened a connection on its own
    loop and closed it, the second found it in the pool. So a test that runs one
    download and stops proves nothing — this one runs the read twice, which is
    what the worker does every time, and asserts the second is not affected by
    the first having happened.
    """

    def test_the_handoff_survives_being_read_twice(self):
        from unittest.mock import MagicMock, patch

        from app.modules.vault.services import take_download_handoff

        redis = MagicMock()
        redis.getdel = MagicMock(side_effect=[json.dumps({"url": "https://example.com/1"}), None])
        with patch("app.modules.vault.services.sync_redis_client", redis):
            first = take_download_handoff("one")
            second = take_download_handoff("two")

        self.assertEqual("https://example.com/1", first["url"])
        self.assertEqual({}, second, "one-shot per handoff, and the second read is unaffected")

    def test_the_session_read_survives_being_called_twice(self):
        from unittest.mock import MagicMock, patch

        from app.modules.vault import sealing

        redis = MagicMock()
        redis.get = MagicMock(return_value=None)
        with patch.object(sealing, "sync_redis_client", redis):
            self.assertIsNone(sealing.data_key_for_id_sync(5, "tab"))
            self.assertIsNone(sealing.data_key_for_id_sync(5, "tab"))

        self.assertEqual(2, redis.get.call_count, "both reads go to the loop-less client")


class TheWebKeepsItsAsyncClientTests(unittest.TestCase):
    """The fix is not "stop using async Redis"."""

    def test_the_web_still_reads_the_session_asynchronously(self):
        sealing = SEALING.read_text(encoding="utf-8")
        self.assertIn("await redis_client.get(collection_key(", sealing)

    def test_the_queue_side_is_still_async(self):
        """Only the worker reads; the web still writes the handoff from a long
        lived loop, and making that synchronous would block the event loop."""
        services = SERVICES.read_text(encoding="utf-8")
        self.assertIn("await redis_client.set", services)


if __name__ == "__main__":
    unittest.main()
