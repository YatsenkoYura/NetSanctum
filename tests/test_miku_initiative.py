import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.modules.miku.briefing import build_briefing
from app.modules.miku.consolidate import consolidate_old_conversations
from app.modules.miku.deep_link import is_allowed_open_path
from app.modules.miku.models import (
    MikuCascadeLog,
    MikuConversation,
    MikuConversationMessage,
    MikuEpisodeMemory,
    MikuTask,
)


class FakeAsyncSession:
    """Minimal async facade over a sync Session for briefing/consolidate tests."""

    def __init__(self, session: Session):
        self.session = session
        self.added = []

    def add(self, instance):
        self.session.add(instance)
        self.added.append(instance)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def flush(self):
        self.session.flush()


class DeepLinkTests(unittest.TestCase):
    def test_allowed_paths_pass(self):
        self.assertTrue(is_allowed_open_path("alllib", "/alllib/reader/9"))
        self.assertTrue(is_allowed_open_path("video_archiver", "/video-archiver/dashboard?miku_item=1"))
        self.assertTrue(is_allowed_open_path("music", "/music/dashboard?miku_item=7"))

    def test_foreign_or_remote_paths_fail(self):
        self.assertFalse(is_allowed_open_path("music", "https://evil.example/x"))
        self.assertFalse(is_allowed_open_path("music", "/vault/dashboard"))
        self.assertFalse(is_allowed_open_path("unknown_module", "/unknown/dashboard"))
        self.assertFalse(is_allowed_open_path("music", "/music/../vault/private"))
        self.assertFalse(is_allowed_open_path("music", None))

    def test_search_open_paths_match_the_contract(self):
        import re

        from app.modules.miku.deep_link import OPEN_PATH_PREFIXES

        for path in (
            "app/modules/alllib/search.py",
            "app/modules/music/search.py",
            "app/modules/vault/search.py",
            "app/modules/video_archiver/search.py",
        ):
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            paths = re.findall(r'open_path=(?:f?"([^"]+)"|\(\s*f?"([^"]+)")', content)
            literals = [first or second for first, second in paths]
            self.assertTrue(literals, f"no open_path found in {path}")
            for literal in literals:
                base = literal.split("{")[0].split("?")[0]
                self.assertTrue(base.startswith("/"), f"{path}: {literal} is not local")
                self.assertTrue(
                    any(
                        base.startswith(prefix)
                        for prefixes in OPEN_PATH_PREFIXES.values()
                        for prefix in prefixes
                    ),
                    f"{path}: {literal} matches no allowed prefix",
                )


class InitiativeTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine,
            tables=[
                MikuConversation.__table__,
                MikuConversationMessage.__table__,
                MikuEpisodeMemory.__table__,
                MikuTask.__table__,
                MikuCascadeLog.__table__,
            ],
        )
        self.session = Session(self.engine, expire_on_commit=False)
        self.db = FakeAsyncSession(self.session)

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _thread(self, user_id=7, age_hours=30):
        moment = datetime.now(UTC) - timedelta(hours=age_hours)
        thread = MikuConversation(user_id=user_id, title="Zero Escape", created_at=moment)
        self.session.add(thread)
        self.session.flush()
        thread.updated_at = moment
        self.session.add(
            MikuConversationMessage(conversation_id=thread.id, role="user", content="найди финал")
        )
        self.session.add(
            MikuConversationMessage(conversation_id=thread.id, role="assistant", content="Нашла финал.")
        )
        self.session.flush()
        return thread

    def test_stale_thread_becomes_an_episode(self):
        self._thread()
        written = asyncio.run(consolidate_old_conversations(self.db))
        self.assertEqual(1, written)
        episodes = self.session.scalars(select(MikuEpisodeMemory)).all()
        self.assertEqual(1, len(episodes))
        self.assertIn("Zero Escape", episodes[0].summary)
        # Second pass finds nothing new: idempotent.
        written_again = asyncio.run(consolidate_old_conversations(self.db))
        self.assertEqual(0, written_again)

    def test_fresh_thread_is_left_alone(self):
        self._thread(age_hours=1)
        self.assertEqual(0, asyncio.run(consolidate_old_conversations(self.db)))

    def test_briefing_lists_tasks_episodes_and_turns(self):
        self._thread()
        self.session.add(MikuTask(user_id=7, goal="Докачать финал", state="open"))
        self.session.add(
            MikuEpisodeMemory(user_id=7, summary="Смотрел финал Zero Escape", occurred_at=datetime.now(UTC))
        )
        self.session.add(
            MikuCascadeLog(user_id=7, request_id="r1", goal="найди финал", status="done", steps_json={})
        )
        self.session.flush()
        briefing = asyncio.run(build_briefing(self.db, 7))
        self.assertEqual(1, briefing["open_tasks"])
        self.assertTrue(any("Докачать финал" in line for line in briefing["lines"]))

    def test_notifications_push_and_pop(self):
        async def run():
            from app.modules.miku.notifications import pop_notifications, push_notification

            calls = []

            class FakePipeline:
                def lpush(self, *args):
                    calls.append(("lpush", args))
                    return self

                def ltrim(self, *args):
                    calls.append(("ltrim", args))
                    return self

                def expire(self, *args):
                    calls.append(("expire", args))
                    return self

                async def execute(self):
                    return []

            with (
                patch(
                    "app.modules.miku.notifications.redis_client.pipeline",
                    return_value=FakePipeline(),
                ),
                patch(
                    "app.modules.miku.notifications.redis_client.lrange",
                    AsyncMock(return_value=[]),
                ),
                patch(
                    "app.modules.miku.notifications.redis_client.delete",
                    AsyncMock(),
                ),
            ):
                await push_notification(7, "Готово: финал")
                items = await pop_notifications(7)
            return calls, items

        calls, items = asyncio.run(run())
        self.assertTrue([call for call in calls if call[0] == "lpush"])
        self.assertEqual([], items)

    def test_provider_bundle_is_cached_and_invalidated(self):
        from app.modules.miku import providers as provider_module
        from app.modules.miku.providers import invalidate_provider_cache, load_provider_bundle

        async def run():
            with patch.object(provider_module, "resolve_many", AsyncMock(return_value={})) as resolve:
                first = await load_provider_bundle(SimpleNamespace(), 777)
                second = await load_provider_bundle(SimpleNamespace(), 777)
                self.assertEqual(1, resolve.await_count)
                self.assertIs(first, second)
                invalidate_provider_cache(777)
                await load_provider_bundle(SimpleNamespace(), 777)
                self.assertEqual(2, resolve.await_count)

        asyncio.run(run())
        invalidate_provider_cache(777)


if __name__ == "__main__":
    unittest.main()
