import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.undo_v1 import UndoRequest
from app.core.database import Base
from app.core.module_types import IntegrationEffects
from app.modules.miku.cascades import (
    build_undo_plan,
    claim_due_tasks,
    finish_task,
    list_cascades,
    open_task,
    record_cascade,
    reply_outcome,
    steps_from_result,
    undo_cascade_step,
)
from app.modules.miku.models import MikuCascadeLog, MikuTask
from app.modules.miku.schemas import MikuReply

OWNER = SimpleNamespace(id=7)


class AsyncSessionAdapter:
    """The cascade code is async; the test drives a real database through a thin shim."""

    def __init__(self, session: Session):
        self.session = session

    def get_bind(self):
        return self.session.get_bind()

    def begin_nested(self):
        return _Transaction(self.session.begin_nested())

    async def get(self, model, identity):
        return self.session.get(model, identity)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def flush(self):
        self.session.flush()

    def add(self, instance):
        self.session.add(instance)


class _Transaction:
    def __init__(self, transaction):
        self.transaction = transaction

    async def __aenter__(self):
        return self.transaction.__enter__()

    async def __aexit__(self, exc_type, exc, traceback):
        return self.transaction.__exit__(exc_type, exc, traceback)


EFFECTS = {
    "vault_capture_v1": {
        "effect": "create",
        "external_io": False,
        "idempotent": False,
        "reversible": True,
        "undo_integration": "vault.undo.v1",
    },
    "miku_memory_write_v1": {
        "effect": "update",
        "external_io": False,
        "idempotent": False,
        "reversible": True,
        "undo_integration": "miku.memory.undo.v1",
    },
    "search_global_v1": {"effect": "read", "external_io": False, "idempotent": True},
}


class StubRegistry:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def invoke_integration(self, integration_id, payload, context):
        self.calls.append((integration_id, payload))
        return {"status": "deleted"}


class MikuCascadeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[MikuCascadeLog.__table__, MikuTask.__table__],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = AsyncSessionAdapter(self.session)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _record(self, *, exhausted: bool = False):
        return asyncio_run(
            record_cascade(
                self.db,
                OWNER,
                request_id="req-1",
                goal="найди Re:Zero",
                steps=[
                    {
                        "tool": "search_global_v1",
                        "status": "success",
                        "summary": "10",
                        "arguments": {"query": "x"},
                    },
                    {
                        "tool": "vault_capture_v1",
                        "status": "success",
                        "summary": "saved",
                        "arguments": {"url": "https://example.org/a"},
                    },
                ],
                skeleton=["search", "save"],
                answer="Готово" if not exhausted else "Не успела",
                exhausted=exhausted,
                effects=EFFECTS,
            )
        )

    def test_cascade_is_written_with_an_undo_plan(self):
        entry = self._record()
        self.session.flush()
        self.assertEqual("done", entry.status)
        self.assertEqual("req-1", entry.request_id)
        plan = entry.undo_json
        self.assertEqual(["vault_capture_v1"], [item["tool"] for item in plan["reversible"]])
        self.assertEqual("vault.undo.v1", plan["reversible"][0]["undo_integration"])
        self.assertEqual(["search", "save"], entry.skeleton_json)

    def test_undo_runs_the_declared_integration(self):
        self._record()
        self.session.flush()
        registry = StubRegistry()
        result = asyncio_run(undo_cascade_step(self.db, OWNER, 1, 0, registry))
        self.assertEqual("deleted", result["result"]["status"])
        self.assertEqual("vault.undo.v1", registry.calls[0][0])
        self.assertEqual(
            {"undo": True, "arguments": {"url": "https://example.org/a"}},
            registry.calls[0][1],
        )

    def test_undo_of_another_owner_is_not_found(self):
        self._record()
        self.session.flush()
        stranger = SimpleNamespace(id=99)
        with self.assertRaises(LookupError):
            asyncio_run(undo_cascade_step(self.db, stranger, 1, 0, StubRegistry()))

    def test_undo_out_of_range_is_refused(self):
        self._record()
        self.session.flush()
        with self.assertRaises(IndexError):
            asyncio_run(undo_cascade_step(self.db, OWNER, 1, 5, StubRegistry()))

    def test_step_without_an_undo_integration_is_only_flagged(self):
        plan = build_undo_plan(
            [{"tool": "miku_memory_write_v1", "status": "success", "arguments": {}}],
            EFFECTS,
        )
        self.assertEqual(["miku_memory_write_v1"], [item["tool"] for item in plan["reversible"]])

    def test_read_only_tools_are_never_planned_for_undo(self):
        plan = build_undo_plan(
            [
                {"tool": "search_global_v1", "status": "success", "arguments": {}},
                {"tool": "read", "status": "success", "arguments": {}},
                {"tool": "act", "status": "success", "arguments": {}},
            ],
            EFFECTS,
        )
        self.assertEqual({"reversible": [], "irreversible": []}, plan)

    def test_memory_write_and_capture_are_both_reversible(self):
        plan = build_undo_plan(
            [
                {"tool": "miku_memory_write_v1", "status": "success", "arguments": {"key": "genre"}},
                {"tool": "vault_capture_v1", "status": "success", "arguments": {"url": "https://a"}},
            ],
            EFFECTS,
        )
        self.assertEqual([], plan["irreversible"])
        self.assertEqual(
            ["miku.memory.undo.v1", "vault.undo.v1"],
            [item["undo_integration"] for item in plan["reversible"]],
        )

    def test_exhausted_turn_parks_the_goal_for_later(self):
        self._record(exhausted=True)
        self.session.flush()
        asyncio_run(open_task(self.db, OWNER, "догоняй Re:Zero", delay_minutes=5))
        self.session.flush()
        tasks = self.session.scalars(select(MikuTask)).all()
        self.assertEqual(1, len(tasks))
        self.assertEqual("open", tasks[0].state)
        due_at = tasks[0].due_at
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=UTC)
        self.assertGreater(due_at, datetime.now(UTC))

    def test_due_tasks_are_claimed_once(self):
        asyncio_run(open_task(self.db, OWNER, "почини", delay_minutes=-1))
        self.session.flush()
        claimed = asyncio_run(claim_due_tasks(self.db))
        self.assertEqual(1, len(claimed))
        self.assertEqual("running", claimed[0].state)
        self.assertEqual(1, claimed[0].attempts)
        self.assertEqual([], asyncio_run(claim_due_tasks(self.db)))

    def test_task_gives_up_after_too_many_attempts(self):
        asyncio_run(open_task(self.db, OWNER, "сломано", delay_minutes=-1))
        self.session.flush()
        task = asyncio_run(claim_due_tasks(self.db))[0]
        for _ in range(4):
            task.attempts += 1
        asyncio_run(finish_task(self.db, task, done=False, error="RuntimeError"))
        self.assertEqual("failed", task.state)
        self.assertEqual("RuntimeError", task.last_error)

    def test_task_retries_with_backoff_before_giving_up(self):
        asyncio_run(open_task(self.db, OWNER, "ещё раз", delay_minutes=-1))
        self.session.flush()
        task = asyncio_run(claim_due_tasks(self.db))[0]
        asyncio_run(finish_task(self.db, task, done=False, error="boom"))
        self.assertEqual("open", task.state)
        due_at = task.due_at if task.due_at.tzinfo else task.due_at.replace(tzinfo=UTC)
        self.assertGreater(due_at, datetime.now(UTC) + timedelta(minutes=1))

    def test_cascade_listing_is_owner_scoped_and_newest_first(self):
        self._record()
        self.session.flush()
        asyncio_run(
            record_cascade(
                self.db,
                OWNER,
                request_id="req-2",
                goal="второе",
                steps=[],
                skeleton=[],
                answer="да",
                exhausted=False,
                effects={},
            )
        )
        asyncio_run(
            record_cascade(
                self.db,
                SimpleNamespace(id=8),
                request_id="req-3",
                goal="чужое",
                steps=[],
                skeleton=[],
                answer="нет",
                exhausted=False,
                effects={},
            )
        )
        self.session.flush()
        rows = asyncio_run(list_cascades(self.db, OWNER))
        self.assertEqual(["второе", "найди Re:Zero"], [row["goal"] for row in rows])
        self.assertEqual("done", rows[0]["status"])
        self.assertIn("reversible", rows[0]["undo"])

    def test_step_records_are_trimmed_for_storage(self):
        class Step:
            def __init__(self, tool, status, summary, arguments):
                self.tool = tool
                self.status = status
                self.summary = summary
                self.arguments = arguments

        class Turn:
            steps: ClassVar[list] = [
                Step("read", "success", "прочитала " * 50, {"ref": "result:1", "max_chars": 8000}),
                Step("fetch", "error", "не вышло", {"url": "https://example.org/" + "x" * 900}),
                Step("search", "success", "найдено", {"filters": {"a": "b" * 900}, "tags": ["x"] * 50}),
            ]

        rows = steps_from_result(Turn())
        self.assertEqual("read", rows[0]["tool"])
        self.assertLessEqual(len(rows[0]["summary"]), 300)
        self.assertEqual(8000, rows[0]["arguments"]["max_chars"])
        self.assertLessEqual(len(rows[1]["arguments"]["url"]), 500)
        # an oversized nested object is replaced by its size, not sliced apart
        self.assertEqual({"truncated_bytes": 909}, rows[2]["arguments"]["filters"])
        self.assertEqual(50, len(rows[2]["arguments"]["tags"]))

    def test_reply_outcome_stays_small(self):
        reply = MikuReply(command="find", text="найдено")
        outcome = reply_outcome(reply)
        self.assertEqual({"command", "references", "client_action", "question"}, set(outcome))
        self.assertEqual(0, outcome["references"])


class UndoContractTests(unittest.TestCase):
    def test_undo_takes_the_original_arguments_and_a_flag(self):
        request = UndoRequest(arguments={"key": "genre", "url": "https://a"})
        self.assertTrue(request.undo)
        self.assertEqual({"key": "genre", "url": "https://a"}, request.arguments)

    def test_undo_defaults_to_no_arguments(self):
        self.assertEqual({}, UndoRequest().arguments)

    def test_oversized_undo_arguments_are_refused(self):
        with self.assertRaises(ValidationError):
            UndoRequest(arguments={"blob": "x" * 4_000})


class IntegrationEffectsTests(unittest.TestCase):
    def test_effects_carry_reversibility_defaults(self):
        effects = IntegrationEffects()
        self.assertFalse(effects.reversible)
        self.assertIsNone(effects.undo_integration)

    def test_reversible_effect_names_its_undo(self):
        effects = IntegrationEffects(reversible=True, undo_integration="vault.delete.v1")
        self.assertTrue(effects.reversible)
        self.assertEqual("vault.delete.v1", effects.undo_integration)


def asyncio_run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


if __name__ == "__main__":
    unittest.main()
