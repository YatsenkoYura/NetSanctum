import asyncio
import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.contracts.planner_v1 import (
    PlannerCompleteTaskRequest,
    PlannerCreateEventRequest,
    PlannerCreateTaskRequest,
    PlannerListEventsRequest,
    PlannerListTasksRequest,
    PlannerResolveSpaceRequest,
    PlannerSnoozeTaskRequest,
    PlannerTodayRequest,
)
from app.contracts.undo_v1 import UndoRequest
from app.contracts.vault_spaces_v1 import VaultSpace, VaultSpacesResult
from app.core.database import Base
from app.core.module_types import IntegrationContext
from app.modules.miku.briefing import build_briefing
from app.modules.miku.models import MikuCascadeLog, MikuEpisodeMemory, MikuTask
from app.modules.planner import integrations
from app.modules.planner.models import PlannerEvent, PlannerTask
from app.modules.search.models import SearchRefreshOutbox


class PlannerSession:
    def __init__(self, session: Session):
        self.session = session

    def add(self, instance):
        self.session.add(instance)

    async def delete(self, instance):
        self.session.delete(instance)

    async def commit(self):
        self.session.commit()

    async def refresh(self, instance):
        self.session.refresh(instance)

    async def execute(self, statement, parameters=None):
        return self.session.execute(statement, parameters or {})

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)


SPACES = VaultSpacesResult(
    spaces=[
        VaultSpace(kind="collection", id=1, name="Работа", color="blue", path="Работа"),
        VaultSpace(kind="folder", id=2, name="Проект X", path="Работа / Проект X"),
    ]
)


class FakeRegistry:
    def __init__(self, spaces: VaultSpacesResult | None = SPACES):
        self.spaces = spaces

    async def invoke_integration(self, integration_id, payload, context):
        assert integration_id == "vault.spaces.v1"
        if self.spaces is None:
            raise RuntimeError("vault is off")
        assert payload == {"include_folders": True, "include_archived": False, "collection_id": None}
        return self.spaces.model_dump(mode="json")


class PlannerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                PlannerTask.__table__,
                PlannerEvent.__table__,
                MikuTask.__table__,
                MikuEpisodeMemory.__table__,
                MikuCascadeLog.__table__,
                SearchRefreshOutbox.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.context = IntegrationContext(
            session=PlannerSession(self.session),
            user=SimpleNamespace(id=7),
            registry=FakeRegistry(),
            consumer_id="miku",
        )

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_create_task_with_space_name(self):
        result = asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(
                    title="Позвонить",
                    due_at="2026-10-05T09:00:00+03:00",
                    remind=True,
                    space="проект x",
                ),
                self.context,
            )
        )
        self.assertEqual("completed", result.status)
        self.assertIsNotNone(result.task_id)
        self.assertIn("2026-10-05 06:00 UTC", result.message)
        self.assertIn("Проект X", result.message)

    def test_create_task_unknown_space_hints_known(self):
        result = asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(title="Дело", space="Марс"),
                self.context,
            )
        )
        self.assertEqual("invalid", result.status)
        self.assertIn("Работа", result.message)

    def test_create_task_bad_date_explains_format(self):
        result = asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(title="Дело", due_at="завтра в девять"),
                self.context,
            )
        )
        self.assertEqual("invalid", result.status)
        self.assertIn("ISO", result.message)

    def test_create_task_without_vault_degrades_to_hint(self):
        context = IntegrationContext(
            session=self.context.session,
            user=self.context.user,
            registry=FakeRegistry(spaces=None),
            consumer_id="miku",
        )
        result = asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(title="Дело", space="Работа"),
                context,
            )
        )
        self.assertEqual("invalid", result.status)
        self.assertIn("without a space", result.message)

    def test_list_tasks_views(self):
        asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(title="Старое", due_at="2020-01-01T09:00:00+03:00"),
                self.context,
            )
        )
        asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(title="Без срока"),
                self.context,
            )
        )
        overdue = asyncio.run(integrations.list_tasks(PlannerListTasksRequest(view="overdue"), self.context))
        self.assertEqual(["Старое"], [task.title for task in overdue.tasks])
        today = asyncio.run(integrations.list_tasks(PlannerListTasksRequest(view="today"), self.context))
        titles = [task.title for task in today.tasks]
        self.assertIn("Старое", titles)
        self.assertIn("Без срока", titles)

    def test_complete_unknown_id_is_invalid(self):
        result = asyncio.run(
            integrations.complete_task(PlannerCompleteTaskRequest(task_id=404), self.context)
        )
        self.assertEqual("invalid", result.status)
        self.assertIn("List tasks first", result.message)

    def test_complete_recurring_reports_next(self):
        created = asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(
                    title="Полив",
                    due_at="2026-10-05T09:00:00+03:00",
                    recurrence="daily",
                ),
                self.context,
            )
        )
        assert created.task_id is not None
        result = asyncio.run(
            integrations.complete_task(PlannerCompleteTaskRequest(task_id=created.task_id), self.context)
        )
        self.assertEqual("completed", result.status)
        self.assertIsNotNone(result.next_id)
        self.assertIn("Next instance", result.message)

    def test_undo_create_removes_newest_match(self):
        created = asyncio.run(
            integrations.create_task(PlannerCreateTaskRequest(title="Временное"), self.context)
        )
        assert created.task_id is not None
        undone = asyncio.run(
            integrations.undo_planner_write(UndoRequest(arguments={"title": "Временное"}), self.context)
        )
        self.assertEqual("undone", undone.status)
        again = asyncio.run(
            integrations.undo_planner_write(UndoRequest(arguments={"title": "Временное"}), self.context)
        )
        self.assertEqual("missing", again.status)

    def test_undo_complete_reopens_and_drops_twin(self):
        created = asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(
                    title="Отчет",
                    due_at="2026-10-05T09:00:00+03:00",
                    recurrence="weekly",
                ),
                self.context,
            )
        )
        assert created.task_id is not None
        asyncio.run(
            integrations.complete_task(PlannerCompleteTaskRequest(task_id=created.task_id), self.context)
        )
        undone = asyncio.run(
            integrations.undo_planner_write(UndoRequest(arguments={"task_id": created.task_id}), self.context)
        )
        self.assertEqual("undone", undone.status)
        remaining = asyncio.run(integrations.list_tasks(PlannerListTasksRequest(view="all"), self.context))
        self.assertEqual(["Отчет"], [task.title for task in remaining.tasks])
        self.assertEqual(["todo"], [task.status for task in remaining.tasks])

    def test_snooze_moves_reminder(self):
        created = asyncio.run(integrations.create_task(PlannerCreateTaskRequest(title="Дело"), self.context))
        assert created.task_id is not None
        result = asyncio.run(
            integrations.snooze_task(
                PlannerSnoozeTaskRequest(task_id=created.task_id, remind_at="2026-10-06T09:00:00+03:00"),
                self.context,
            )
        )
        self.assertEqual("completed", result.status)
        self.assertIn("2026-10-06 06:00 UTC", result.message)

    def test_create_and_list_events(self):
        created = asyncio.run(
            integrations.create_event(
                PlannerCreateEventRequest(
                    title="Созвон",
                    starts_at="2026-10-06T10:00:00+03:00",
                    location="кабинет",
                    remind=True,
                ),
                self.context,
            )
        )
        self.assertEqual("completed", created.status)
        self.assertIsNotNone(created.event_id)
        listed = asyncio.run(integrations.list_events(PlannerListEventsRequest(days=30), self.context))
        self.assertEqual(["Созвон"], [event.title for event in listed.events])

    def test_today_orders_overdue_first(self):
        asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(title="Просрочка", due_at="2020-01-01T09:00:00+03:00"),
                self.context,
            )
        )
        result = asyncio.run(integrations.today(PlannerTodayRequest(), self.context))
        self.assertEqual(1, result.overdue_count)
        self.assertTrue(result.lines[0].startswith("OVERDUE:"))
        self.assertLessEqual(len(result.lines), 12)

    def test_resolve_space(self):
        found = asyncio.run(
            integrations.resolve_space(PlannerResolveSpaceRequest(name="работа"), self.context)
        )
        self.assertEqual("completed", found.status)
        assert found.space is not None
        self.assertEqual(1, found.space.id)
        missing = asyncio.run(
            integrations.resolve_space(PlannerResolveSpaceRequest(name="Марс"), self.context)
        )
        self.assertEqual("invalid", missing.status)


class PlannerBriefingTests(unittest.TestCase):
    """The morning briefing gains a planner agenda section through the registry."""

    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                PlannerTask.__table__,
                PlannerEvent.__table__,
                MikuTask.__table__,
                MikuEpisodeMemory.__table__,
                MikuCascadeLog.__table__,
                SearchRefreshOutbox.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.planner_context = IntegrationContext(
            session=PlannerSession(self.session),
            user=SimpleNamespace(id=7),
            registry=FakeRegistry(),
            consumer_id="planner",
        )

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_briefing_contains_planner_agenda(self):
        asyncio.run(
            integrations.create_task(
                PlannerCreateTaskRequest(title="Просрочка", due_at="2020-01-01T09:00:00+03:00"),
                self.planner_context,
            )
        )

        async def route(integration_id, payload, context):
            assert integration_id == "planner.today.v1"
            result = await integrations.today(PlannerTodayRequest(), self.planner_context)
            return result.model_dump(mode="json")

        registry = SimpleNamespace(
            has_integration=lambda iid: iid == "planner.today.v1", invoke_integration=route
        )
        briefing = asyncio.run(
            build_briefing(PlannerSession(self.session), 7, registry=registry, user=SimpleNamespace(id=7))
        )
        self.assertTrue(any("Планы: просрочено 1" in line for line in briefing["lines"]))
        self.assertTrue(any("Просрочка" in line for line in briefing["lines"]))

    def test_briefing_survives_missing_planner(self):
        registry = SimpleNamespace(has_integration=lambda iid: False)
        briefing = asyncio.run(
            build_briefing(PlannerSession(self.session), 7, registry=registry, user=SimpleNamespace(id=7))
        )
        self.assertFalse(any("Планы:" in line for line in briefing["lines"]))


if __name__ == "__main__":
    unittest.main()
