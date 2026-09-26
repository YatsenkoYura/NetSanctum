import asyncio
import unittest
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.miku_memory_v1 import (
    MikuMemorySearchRequest,
    MikuMemoryWriteRequest,
)
from app.core.database import Base
from app.core.module_types import IntegrationContext, IntegrationUnavailableError
from app.modules.miku.integrations import search_memory, write_memory
from app.modules.miku.models import MikuEpisodeMemory, MikuProfileMemory


class AsyncTransaction:
    def __init__(self, transaction: AbstractContextManager):
        self.transaction = transaction

    async def __aenter__(self):
        return self.transaction.__enter__()

    async def __aexit__(self, exc_type, exc, traceback):
        return self.transaction.__exit__(exc_type, exc, traceback)


class AsyncSessionAdapter:
    def __init__(self, session: Session):
        self.session = session

    def get_bind(self):
        return self.session.get_bind()

    def begin_nested(self):
        return AsyncTransaction(self.session.begin_nested())

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

    async def delete(self, instance):
        self.session.delete(instance)


class MikuMemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(
            self.engine, tables=[MikuProfileMemory.__table__, MikuEpisodeMemory.__table__]
        )
        self.session = self.session = Session(self.engine, expire_on_commit=False)
        self.context = IntegrationContext(
            session=AsyncSessionAdapter(self.session),
            user=SimpleNamespace(id=7),
            registry=SimpleNamespace(),
            consumer_id="miku",
        )

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _write(self, request: MikuMemoryWriteRequest):
        result = asyncio.run(write_memory(request, self.context))
        self.session.flush()
        return result

    def _search(self, request: MikuMemorySearchRequest):
        return asyncio.run(search_memory(request, self.context))

    def test_profile_fact_is_stored_updated_and_deleted(self):
        written = self._write(MikuMemoryWriteRequest(key="favorite_genre", value={"name": "synthwave"}))
        self.assertEqual("written", written.status)
        record = self.session.scalar(select(MikuProfileMemory))
        self.assertEqual("favorite_genre", record.memory_key)
        self.assertEqual({"name": "synthwave"}, record.value_json)
        self.assertEqual(7, record.user_id)

        self._write(MikuMemoryWriteRequest(key="favorite_genre", value={"name": "dark ambient"}))
        self.assertEqual(1, len(self.session.scalars(select(MikuProfileMemory)).all()))
        self.assertEqual(
            {"name": "dark ambient"},
            self.session.scalar(select(MikuProfileMemory)).value_json,
        )

        deleted = self._write(MikuMemoryWriteRequest(op="delete", key="favorite_genre"))
        self.assertEqual("deleted", deleted.status)
        self.assertEqual([], self.session.scalars(select(MikuProfileMemory)).all())
        self.assertEqual(
            "missing",
            self._write(MikuMemoryWriteRequest(op="delete", key="favorite_genre")).status,
        )

    def test_episode_summary_is_stored_and_recalled(self):
        self._write(
            MikuMemoryWriteRequest(
                scope="episodic",
                summary="Пользователь попросил пересказ третьей главы Re:Zero",
                subject="Re:Zero",
                tags=["Пересказ"],
            )
        )
        record = self.session.scalar(select(MikuEpisodeMemory))
        self.assertEqual(["пересказ"], record.tags_json)
        result = self._search(MikuMemorySearchRequest(query="rezero", scopes=["episodic"]))
        self.assertEqual(1, len(result.items))
        self.assertIn("третьей главы", result.items[0].summary)

    def test_recall_filters_by_scope_and_terms(self):
        self._write(MikuMemoryWriteRequest(key="favorite_genre", value={"name": "synthwave"}))
        self._write(MikuMemoryWriteRequest(scope="episodic", summary="Смотрел финал Zero Escape"))
        only_profile = self._search(MikuMemorySearchRequest(scopes=["profile"]))
        self.assertEqual(["favorite_genre"], [item.key for item in only_profile.items])
        no_match = self._search(MikuMemorySearchRequest(query="квантовая физика"))
        self.assertEqual([], no_match.items)
        everything = self._search(MikuMemorySearchRequest())
        self.assertEqual(2, len(everything.items))

    def test_recall_survives_russian_case_endings(self):
        self._write(MikuMemoryWriteRequest(key="food", value={"note": "люблю пиццу"}))
        self._write(MikuMemoryWriteRequest(scope="episodic", summary="Пользователь любит аниме"))
        for query in ("пицца", "пиццу", "ПИЦЦА", "люблю"):
            found = self._search(MikuMemorySearchRequest(query=query))
            self.assertTrue(found.items, f"запрос {query!r} ничего не нашёл")
        anime = self._search(MikuMemorySearchRequest(query="аниме"))
        self.assertEqual(["episodic"], [item.scope for item in anime.items])

    def test_recall_ignores_words_that_only_look_similar(self):
        self._write(MikuMemoryWriteRequest(key="food", value={"note": "люблю пиццу"}))
        unrelated = self._search(MikuMemorySearchRequest(query="квантовая физика"))
        self.assertEqual([], unrelated.items)

    def test_expired_profile_fact_is_not_recalled(self):
        self._write(
            MikuMemoryWriteRequest(
                key="temporary",
                value={"name": "x"},
                expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            )
        )
        self.assertEqual([], self._search(MikuMemorySearchRequest()).items)

    def test_memory_is_scoped_to_the_owner(self):
        self._write(MikuMemoryWriteRequest(key="favorite_genre", value={"name": "synthwave"}))
        other = IntegrationContext(
            session=AsyncSessionAdapter(self.session),
            user=SimpleNamespace(id=8),
            registry=SimpleNamespace(),
            consumer_id="miku",
        )
        self.assertEqual([], asyncio.run(search_memory(MikuMemorySearchRequest(), other)).items)

    def test_payload_validation_prevents_unusable_memories(self):
        with self.assertRaises(ValidationError):
            MikuMemoryWriteRequest(scope="profile")
        with self.assertRaises(ValidationError):
            MikuMemoryWriteRequest(scope="episodic")
        with self.assertRaises(ValidationError):
            MikuMemoryWriteRequest(key="Not A Key", value={})
        with self.assertRaises(ValidationError):
            MikuMemoryWriteRequest(key="big", value={"blob": "x" * 4_000})
        with self.assertRaises(ValidationError):
            MikuMemoryWriteRequest(key="tags", tags=["x" * 60])

    def test_anonymous_context_cannot_touch_memory(self):
        anonymous = IntegrationContext(
            session=AsyncSessionAdapter(self.session),
            user=None,
            registry=SimpleNamespace(),
            consumer_id="miku",
        )
        with self.assertRaises(IntegrationUnavailableError):
            asyncio.run(write_memory(MikuMemoryWriteRequest(key="k", value={}), anonymous))
        with self.assertRaises(IntegrationUnavailableError):
            asyncio.run(search_memory(MikuMemorySearchRequest(), anonymous))


if __name__ == "__main__":
    unittest.main()
