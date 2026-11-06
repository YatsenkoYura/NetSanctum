import asyncio
import datetime
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.contracts.search_documents_v1 import SearchDocumentsRequest
from app.core.database import Base
from app.core.module_types import IntegrationContext
from app.modules.planner import services
from app.modules.planner.models import PlannerEvent, PlannerTask
from app.modules.planner.schemas import EventCreate, TaskCreate, TaskUpdate
from app.modules.planner.search import search_documents
from app.modules.planner.sweep import sweep_due_reminders
from app.modules.search.models import SearchRefreshOutbox

MSK = ZoneInfo("Europe/Moscow")


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


def aware(year, month, day, hour=0, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=datetime.UTC)


class PlannerServicesTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                PlannerTask.__table__,
                PlannerEvent.__table__,
                SearchRefreshOutbox.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = PlannerSession(self.session)
        self.user_id = 7

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_create_task_naive_datetime_means_moscow(self):
        task = asyncio.run(
            services.create_task(
                self.db,
                self.user_id,
                TaskCreate(title="Позвонить", due_at=datetime.datetime(2026, 10, 5, 9, 0)),
            )
        )
        self.assertEqual(aware(2026, 10, 5, 6, 0), task.due_at)

    def test_create_task_aware_datetime_kept(self):
        task = asyncio.run(
            services.create_task(
                self.db,
                self.user_id,
                TaskCreate(title="X", due_at=aware(2026, 10, 5, 9, 0)),
            )
        )
        self.assertEqual(aware(2026, 10, 5, 9, 0), task.due_at)

    def test_other_users_tasks_are_invisible(self):
        asyncio.run(services.create_task(self.db, 999, TaskCreate(title="Чужое")))
        tasks = asyncio.run(services.list_tasks(self.db, self.user_id))
        self.assertEqual([], tasks)

    def test_overdue_filter(self):
        asyncio.run(
            services.create_task(self.db, self.user_id, TaskCreate(title="Старое", due_at=aware(2020, 1, 1)))
        )
        asyncio.run(
            services.create_task(self.db, self.user_id, TaskCreate(title="Будущее", due_at=aware(2030, 1, 1)))
        )
        overdue = asyncio.run(services.list_tasks(self.db, self.user_id, overdue_only=True))
        self.assertEqual(["Старое"], [task.title for task in overdue])

    def test_space_filter(self):
        asyncio.run(
            services.create_task(
                self.db,
                self.user_id,
                TaskCreate(title="По работе", space_kind="collection", space_id=3, space_name="Работа"),
            )
        )
        asyncio.run(services.create_task(self.db, self.user_id, TaskCreate(title="Без места")))
        scoped = asyncio.run(services.list_tasks(self.db, self.user_id, space_kind="collection", space_id=3))
        self.assertEqual(["По работе"], [task.title for task in scoped])

    def test_complete_plain_task(self):
        task = asyncio.run(services.create_task(self.db, self.user_id, TaskCreate(title="Разовое")))
        spawned = asyncio.run(services.complete_task(self.db, task))
        self.assertIsNone(spawned)
        self.assertEqual("done", task.status)
        self.assertIsNotNone(task.completed_at)

    def test_complete_recurring_task_spawns_next(self):
        task = asyncio.run(
            services.create_task(
                self.db,
                self.user_id,
                TaskCreate(title="Каждый день", due_at=aware(2026, 10, 5, 6, 0), recurrence="daily"),
            )
        )
        spawned = asyncio.run(services.complete_task(self.db, task))
        self.assertIsNotNone(spawned)
        assert spawned is not None
        self.assertEqual(aware(2026, 10, 6, 6, 0), spawned.due_at)
        self.assertEqual("daily", spawned.recurrence)
        self.assertEqual("todo", spawned.status)

    def test_next_occurrence_rules(self):
        friday = aware(2026, 10, 2, 6, 0)  # a Friday
        self.assertEqual(aware(2026, 10, 5, 6, 0), services.next_occurrence(friday, "weekdays"))
        self.assertEqual(aware(2026, 10, 9, 6, 0), services.next_occurrence(friday, "weekly"))
        jan31 = aware(2026, 1, 31, 6, 0)
        self.assertEqual(aware(2026, 2, 28, 6, 0), services.next_occurrence(jan31, "monthly"))
        self.assertIsNone(services.next_occurrence(friday, "none"))

    def test_update_task_reopens(self):
        task = asyncio.run(services.create_task(self.db, self.user_id, TaskCreate(title="Дело")))
        asyncio.run(services.complete_task(self.db, task))
        updated = asyncio.run(services.update_task(self.db, task, TaskUpdate(status="todo")))
        self.assertEqual("todo", updated.status)
        self.assertIsNone(updated.completed_at)

    def test_list_tasks_due_range(self):
        asyncio.run(
            services.create_task(
                self.db, self.user_id, TaskCreate(title="Внутри", due_at=aware(2026, 10, 6, 9, 0))
            )
        )
        asyncio.run(
            services.create_task(
                self.db, self.user_id, TaskCreate(title="Раньше", due_at=aware(2026, 9, 1, 9, 0))
            )
        )
        asyncio.run(
            services.create_task(
                self.db, self.user_id, TaskCreate(title="Позже", due_at=aware(2026, 11, 1, 9, 0))
            )
        )
        scoped = asyncio.run(
            services.list_tasks(
                self.db,
                self.user_id,
                due_after=aware(2026, 10, 1),
                due_before=aware(2026, 10, 31, 23, 59),
            )
        )
        self.assertEqual(["Внутри"], [task.title for task in scoped])

    def test_list_events_starts_range(self):
        asyncio.run(
            services.create_event(
                self.db, self.user_id, EventCreate(title="Внутри", starts_at=aware(2026, 10, 6, 7, 0))
            )
        )
        asyncio.run(
            services.create_event(
                self.db, self.user_id, EventCreate(title="Раньше", starts_at=aware(2026, 9, 1, 7, 0))
            )
        )
        scoped = asyncio.run(
            services.list_events(
                self.db,
                self.user_id,
                statuses=("active",),
                starts_after=aware(2026, 10, 1),
                starts_before=aware(2026, 10, 31, 23, 59),
            )
        )
        self.assertEqual(["Внутри"], [event.title for event in scoped])

    def test_event_crud(self):
        event = asyncio.run(
            services.create_event(
                self.db,
                self.user_id,
                EventCreate(
                    title="Созвон",
                    starts_at=datetime.datetime(2026, 10, 6, 10, 0),
                    location="кабинет",
                ),
            )
        )
        self.assertEqual(aware(2026, 10, 6, 7, 0), event.starts_at)
        upcoming = asyncio.run(
            services.list_events(self.db, self.user_id, statuses=("active",), starts_after=aware(2026, 10, 1))
        )
        self.assertEqual(["Созвон"], [item.title for item in upcoming])
        self.assertIsNotNone(asyncio.run(services.get_event(self.db, self.user_id, event.id)))
        self.assertIsNone(asyncio.run(services.get_event(self.db, 999, event.id)))
        asyncio.run(services.delete_event(self.db, event))
        remaining = asyncio.run(services.list_events(self.db, self.user_id))
        self.assertEqual([], remaining)


class PlannerRangeParsingTests(unittest.TestCase):
    def test_plain_day_bounds_use_moscow_wall_time(self):
        from app.modules.planner.router import _parse_range_bound

        start = _parse_range_bound("2026-10-05", is_end=False)
        end = _parse_range_bound("2026-10-05", is_end=True)
        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        assert start is not None and end is not None
        # 2026-10-05 00:00 MSK (+03:00) is the previous day 21:00 UTC.
        self.assertEqual(aware(2026, 10, 4, 21, 0), start)
        self.assertEqual(datetime.datetime(2026, 10, 5, 20, 59, 59, 999999, tzinfo=datetime.UTC), end)

    def test_iso_datetime_kept_as_moment(self):
        from app.modules.planner.router import _parse_range_bound

        moment = _parse_range_bound("2026-10-05T09:00:00+03:00", is_end=False)
        self.assertEqual(aware(2026, 10, 5, 6, 0), moment)
        self.assertIsNone(_parse_range_bound(None, is_end=False))
        self.assertIsNone(_parse_range_bound("  ", is_end=True))


class PlannerSweepTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                PlannerTask.__table__,
                PlannerEvent.__table__,
                SearchRefreshOutbox.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = PlannerSession(self.session)
        self.user_id = 7
        self.pushed = []

        async def fake_push(user_id, text, **kwargs):
            self.pushed.append((user_id, text))

        self._patcher = unittest.mock.patch(
            "app.modules.planner.sweep.push_notification", side_effect=fake_push
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.session.close()
        self.engine.dispose()

    def test_due_reminder_pushed_once(self):
        asyncio.run(
            services.create_task(
                self.db,
                self.user_id,
                TaskCreate(title="Позвонить", remind_at=aware(2020, 1, 1)),
            )
        )
        pushed = asyncio.run(sweep_due_reminders(self.db))
        self.assertEqual(1, pushed)
        self.assertEqual(1, len(self.pushed))
        self.assertIn("Позвонить", self.pushed[0][1])
        self.assertIn("inbox", self.pushed[0][1])
        pushed_again = asyncio.run(sweep_due_reminders(self.db))
        self.assertEqual(0, pushed_again)
        self.assertEqual(1, len(self.pushed))

    def test_future_reminder_untouched(self):
        asyncio.run(
            services.create_task(
                self.db,
                self.user_id,
                TaskCreate(title="Потом", remind_at=aware(2030, 1, 1)),
            )
        )
        self.assertEqual(0, asyncio.run(sweep_due_reminders(self.db)))
        self.assertEqual([], self.pushed)

    def test_due_event_pushed_with_space(self):
        asyncio.run(
            services.create_event(
                self.db,
                self.user_id,
                EventCreate(
                    title="Созвон",
                    starts_at=aware(2026, 10, 6, 7, 0),
                    remind_at=aware(2020, 1, 1),
                    space_kind="collection",
                    space_id=1,
                    space_name="Работа",
                ),
            )
        )
        self.assertEqual(1, asyncio.run(sweep_due_reminders(self.db)))
        self.assertIn("Работа", self.pushed[0][1])

    def test_recurring_past_event_rolls_forward(self):
        asyncio.run(
            services.create_event(
                self.db,
                self.user_id,
                EventCreate(
                    title="Стендап",
                    starts_at=aware(2020, 1, 6, 7, 0),
                    ends_at=aware(2020, 1, 6, 7, 15),
                    recurrence="daily",
                ),
            )
        )
        asyncio.run(sweep_due_reminders(self.db))
        events = asyncio.run(services.list_events(self.db, self.user_id))
        statuses = sorted(event.status for event in events)
        self.assertEqual(["active", "done"], statuses)
        following = next(event for event in events if event.status == "active")
        self.assertEqual(aware(2020, 1, 7, 7, 0), following.starts_at)
        self.assertEqual(aware(2020, 1, 7, 7, 15), following.ends_at)

    def test_plain_past_event_left_alone(self):
        asyncio.run(
            services.create_event(
                self.db,
                self.user_id,
                EventCreate(title="Было", starts_at=aware(2020, 1, 6, 7, 0)),
            )
        )
        asyncio.run(sweep_due_reminders(self.db))
        events = asyncio.run(services.list_events(self.db, self.user_id))
        self.assertEqual(["active"], [event.status for event in events])


class PlannerSearchDocumentsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                PlannerTask.__table__,
                PlannerEvent.__table__,
                SearchRefreshOutbox.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.context = IntegrationContext(
            session=PlannerSession(self.session),
            user=SimpleNamespace(id=7),
            registry=SimpleNamespace(),
            consumer_id="search",
        )

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def test_publishes_tasks_and_events(self):
        asyncio.run(
            services.create_task(
                PlannerSession(self.session),
                7,
                TaskCreate(title="Купить молоко", notes="2 литра", space_name="Дом"),
            )
        )
        asyncio.run(
            services.create_event(
                PlannerSession(self.session),
                7,
                EventCreate(title="Созвон", starts_at=aware(2026, 10, 6, 7, 0)),
            )
        )
        asyncio.run(services.create_task(PlannerSession(self.session), 999, TaskCreate(title="Чужое")))
        result = asyncio.run(search_documents(SearchDocumentsRequest(limit=10), self.context))
        self.assertEqual("planner", result.module_id)
        by_id = {doc.document_id: doc for doc in result.documents}
        self.assertIn("task-1", by_id)
        self.assertIn("event-1", by_id)
        self.assertNotIn("task-2", by_id)
        self.assertEqual("Купить молоко", by_id["task-1"].title)
        self.assertIn("2 литра", by_id["task-1"].body or "")
        self.assertIn("Дом", by_id["task-1"].keywords)
        self.assertEqual("/planner", by_id["task-1"].open_path)
        self.assertIsNone(result.next_offset)

    def test_pagination_and_cancelled_excluded(self):
        db = PlannerSession(self.session)
        for index in range(3):
            asyncio.run(services.create_task(db, 7, TaskCreate(title=f"Дело {index}")))
        doomed = asyncio.run(services.create_task(db, 7, TaskCreate(title="Отмена")))
        asyncio.run(services.update_task(db, doomed, TaskUpdate(status="cancelled")))
        first = asyncio.run(search_documents(SearchDocumentsRequest(limit=2), self.context))
        self.assertEqual(2, len(first.documents))
        self.assertEqual(2, first.next_offset)
        second = asyncio.run(search_documents(SearchDocumentsRequest(offset=2, limit=2), self.context))
        self.assertEqual(1, len(second.documents))
        self.assertIsNone(second.next_offset)
        titles = [doc.title for doc in first.documents + second.documents]
        self.assertNotIn("Отмена", titles)


class SweepArmingTests(unittest.TestCase):
    def test_arms_exactly_once(self):
        from app.modules.planner import tasks as planner_tasks

        store = {}

        async def fake_set(key, value, ex=None, nx=False):
            if nx and key in store:
                return None
            store[key] = value
            return True

        async def run():
            applied = []
            with (
                unittest.mock.patch.object(planner_tasks, "redis_client", SimpleNamespace(set=fake_set)),
                unittest.mock.patch.object(
                    planner_tasks.sweep_reminders,
                    "apply_async",
                    side_effect=lambda **kwargs: applied.append(kwargs),
                ),
            ):
                await planner_tasks.ensure_sweep_armed()
                await planner_tasks.ensure_sweep_armed()
            return applied

        applied = asyncio.run(run())
        self.assertEqual([{"countdown": 60}], applied)

    def test_broken_redis_never_raises(self):
        from app.modules.planner import tasks as planner_tasks

        async def failing_set(*args, **kwargs):
            raise ConnectionError("redis is down")

        async def run():
            with unittest.mock.patch.object(planner_tasks, "redis_client", SimpleNamespace(set=failing_set)):
                await planner_tasks.ensure_sweep_armed()

        asyncio.run(run())

    def test_worker_ready_kicks_ensure(self):
        from unittest.mock import Mock, patch

        from app.modules.planner import tasks as planner_tasks

        # Worker code never awaits: the armed gate runs on the synchronous client.
        with patch.object(planner_tasks, "ensure_sweep_armed_sync", Mock()) as ensure:
            planner_tasks.arm_sweep_on_worker_ready()
        ensure.assert_called_once_with()

    def test_the_sweep_task_does_not_wrap_its_work_in_asyncio_run(self):
        import ast

        from app.modules.planner import tasks as planner_tasks

        tree = ast.parse(Path(planner_tasks.__file__).read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        # `asyncio.run` per pass hands the async pool a different loop every minute,
        # which is what stopped the sweep in production.
        self.assertNotIn("run", called)
        self.assertNotIn("AsyncSessionLocal", Path(planner_tasks.__file__).read_text())

    def test_the_sync_sweep_rolls_repeating_events_like_the_async_one(self):
        from app.modules.planner.sweep import sweep_due_reminders_sync

        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.engine.dispose)

        now = datetime.datetime.now(datetime.UTC)
        past = PlannerEvent(
            user_id=1,
            title="Еженедельный созвон",
            starts_at=now - datetime.timedelta(days=7),
            ends_at=now - datetime.timedelta(days=7, hours=-1),
            recurrence="weekly",
            status="active",
        )
        self.session.add(past)
        self.session.commit()

        with patch("app.modules.planner.sweep.push_notification_sync") as push:
            sweep_due_reminders_sync(self.session)
        push.assert_not_called()

        self.session.expire_all()
        self.assertEqual("done", self.session.get(PlannerEvent, past.id).status)
        remaining = self.session.query(PlannerEvent).filter(PlannerEvent.id != past.id).all()
        self.assertEqual(1, len(remaining))
        self.assertEqual("Еженедельный созвон", remaining[0].title)


if __name__ == "__main__":
    unittest.main()
