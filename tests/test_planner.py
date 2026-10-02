import asyncio
import datetime
import unittest
import unittest.mock
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.database import Base
from app.modules.planner import services
from app.modules.planner.models import PlannerEvent, PlannerTask
from app.modules.planner.schemas import EventCreate, TaskCreate, TaskUpdate
from app.modules.planner.sweep import sweep_due_reminders

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
        Base.metadata.create_all(self.engine, tables=[PlannerTask.__table__, PlannerEvent.__table__])
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


class PlannerSweepTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[PlannerTask.__table__, PlannerEvent.__table__])
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


if __name__ == "__main__":
    unittest.main()
