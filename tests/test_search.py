import asyncio
import unittest
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.search_documents_v1 import (
    SearchDocument,
    SearchDocumentsRequest,
    SearchDocumentsResult,
)
from app.contracts.search_query_v1 import GlobalSearchRequest
from app.core.database import Base
from app.core.module_types import IntegrationContext
from app.core.modules import ModuleRegistry
from app.core.security import get_current_user
from app.modules.alllib.models import LibChapter, LibMedia
from app.modules.alllib.search import search_documents as alllib_search_documents
from app.modules.music.models import Song
from app.modules.search.events import register_search_outbox, unregister_search_outbox
from app.modules.search.integrations import (
    _contains_required_terms,
    _postgres_search_statement,
    _score,
    global_search,
    normalize_search_text,
    refresh_index,
)
from app.modules.search.models import SearchDocumentIndex, SearchRefreshOutbox, SearchSyncState
from app.modules.search.router import router
from app.modules.vault.models import VaultItem
from app.modules.video_archiver.models import ArchivedVideo


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

    async def delete(self, instance):
        self.session.delete(instance)

    def add(self, instance):
        self.session.add(instance)


class SearchProviderRegistry:
    def __init__(self):
        self.fail = False
        self.documents = [
            SearchDocument(
                document_id="video-1",
                entity_type="video",
                title="Zero Escape finale",
                body="Final episode",
                updated_at=datetime.now(UTC),
                open_path="/video-archiver/dashboard?miku_item=video-1",
                playable=True,
            ),
            SearchDocument(
                document_id="video-2",
                entity_type="video",
                title="Quiet documentary",
                updated_at=datetime.now(UTC),
                open_path="/video-archiver/dashboard?miku_item=video-2",
                playable=True,
            ),
        ]
        self.record = SimpleNamespace(id="video_archiver")
        self.spec = SimpleNamespace(id="video_archiver.search.documents.v1")

    def integration_providers(self, contract):
        self.last_contract = contract
        return [(self.record, self.spec)]

    async def invoke_integration(self, integration_id, payload, context):
        if self.fail:
            raise RuntimeError("provider unavailable")
        offset = payload["offset"]
        limit = payload["limit"]
        page = self.documents[offset : offset + limit]
        return SearchDocumentsResult(
            module_id="video_archiver",
            documents=page,
            next_offset=offset + limit if offset + limit < len(self.documents) else None,
        ).model_dump(mode="json")


def indexed_document(title: str, *, body: str = "", module_id: str = "vault"):
    normalized_title = normalize_search_text(title)
    return SearchDocumentIndex(
        source_module_id=module_id,
        source_integration_id=f"{module_id}.search.documents.v1",
        document_id=title.casefold().replace(" ", "-"),
        entity_type="note",
        title=title,
        body=body or None,
        keywords_text="",
        normalized_title=normalized_title,
        search_text=normalize_search_text(f"{title} {body}"),
        playable=False,
        readable=False,
        generation="generation",
    )


class SearchContractTests(unittest.TestCase):
    def test_required_terms_match_whole_tokens(self):
        self.assertTrue(_contains_required_terms("zero escape прохождение 1", ["1"]))
        self.assertFalse(_contains_required_terms("zero escape прохождение 21", ["1"]))

    def test_alllib_publishes_chapters_and_data_derived_aliases(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as sync_session:
            media = LibMedia(
                title="Re:Zero",
                slug="94231--rezero-kara-hajimeru",
                media_type="novel",
            )
            media.chapters.append(
                LibChapter(volume="1", number="3", volume_int=1, number_float=3, name="Первая магия")
            )
            sync_session.add(media)
            sync_session.commit()

            result = asyncio.run(
                alllib_search_documents(
                    SearchDocumentsRequest(limit=20),
                    IntegrationContext(
                        session=AsyncSessionAdapter(sync_session),
                        user=SimpleNamespace(id=1),
                        registry=SimpleNamespace(),
                        consumer_id="search",
                    ),
                )
            )

            media_document, chapter_document = result.documents
            self.assertIn("резеро", media_document.keywords)
            self.assertIn("глава 3", chapter_document.title.casefold())
            self.assertEqual(
                f"/alllib/reader/{media.id}?chapter={media.chapters[0].id}",
                chapter_document.open_path,
            )
            self.assertTrue(chapter_document.readable)
        engine.dispose()

    def test_document_rejects_remote_or_ambiguous_open_paths(self):
        for path in ("https://example.com", "//example.com/path", "/safe\\path", "/safe#fragment"):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                SearchDocument(
                    document_id="1",
                    entity_type="note",
                    title="Example",
                    open_path=path,
                )

    def test_search_normalization_and_ranking_handle_russian_and_typos(self):
        exact = indexed_document("Ёжик в тумане")
        fuzzy = indexed_document("Ежик в тумани")
        unrelated = indexed_document("Другой документ", body="совсем иной текст")
        query = normalize_search_text("  ЁЖИК в тумане ")
        tokens = query.split()

        self.assertEqual("ежик в тумане", query)
        self.assertGreater(_score(exact, query, tokens), _score(fuzzy, query, tokens))
        self.assertGreater(_score(fuzzy, query, tokens), _score(unrelated, query, tokens))

    def test_global_search_returns_ranked_bounded_hits(self):
        documents = [
            indexed_document("Zero Escape finale", module_id="video_archiver"),
            indexed_document("Zero Hour"),
            indexed_document("Unrelated"),
        ]
        session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: documents)))
        context = IntegrationContext(
            session=session,
            user=SimpleNamespace(id=1),
            registry=SimpleNamespace(),
            consumer_id="miku",
        )
        with patch("app.modules.search.integrations.refresh_index", AsyncMock(return_value=[])):
            result = asyncio.run(global_search(GlobalSearchRequest(query="zero escape", limit=2), context))

        self.assertEqual(["Zero Escape finale", "Zero Hour"], [item.title for item in result.items])
        self.assertGreater(result.items[0].score, result.items[1].score)

    def test_global_search_filters_generic_exact_terms(self):
        documents = [
            indexed_document("Zero Escape walkthrough 1", module_id="video_archiver"),
            indexed_document("Zero Escape walkthrough 21", module_id="video_archiver"),
        ]
        session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: documents)))
        context = IntegrationContext(
            session=session,
            user=SimpleNamespace(id=1),
            registry=SimpleNamespace(),
            consumer_id="miku",
        )
        with patch("app.modules.search.integrations.refresh_index", AsyncMock(return_value=[])):
            result = asyncio.run(
                global_search(
                    GlobalSearchRequest(query="zero escape walkthrough", required_terms=["1"]),
                    context,
                )
            )

        self.assertEqual(["Zero Escape walkthrough 1"], [item.title for item in result.items])

    def test_postgres_required_terms_use_exact_token_clauses(self):
        without = str(_postgres_search_statement(0).compile(compile_kwargs={"literal_binds": True}))
        with_terms = str(_postgres_search_statement(2).compile(compile_kwargs={"literal_binds": True}))

        self.assertNotIn("string_to_array", without)
        self.assertIn("string_to_array(d.search_text, ' ')", with_terms)
        self.assertIn(":req_0", str(_postgres_search_statement(2)))
        self.assertIn(":req_1", str(_postgres_search_statement(2)))
        self.assertNotIn(":req_2", str(_postgres_search_statement(2)))

    def test_global_search_merges_independent_alias_queries(self):
        documents = [
            indexed_document("Sword Art Online cover", module_id="video_archiver"),
            indexed_document("Мастера меча онлайн 1 серия", module_id="video_archiver"),
        ]
        session = SimpleNamespace(scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: documents)))
        context = IntegrationContext(
            session=session,
            user=SimpleNamespace(id=1),
            registry=SimpleNamespace(),
            consumer_id="miku",
        )
        with patch("app.modules.search.integrations.refresh_index", AsyncMock(return_value=[])):
            result = asyncio.run(
                global_search(
                    GlobalSearchRequest(
                        query="SAO",
                        alternate_queries=["Sword Art Online", "Мастера меча онлайн"],
                    ),
                    context,
                )
            )

        self.assertEqual(
            {"Sword Art Online cover", "Мастера меча онлайн 1 серия"},
            {item.title for item in result.items},
        )

    def test_registry_scopes_index_publishers_away_from_miku(self):
        registry = ModuleRegistry.discover()
        miku_ids = {item["id"] for item in registry.integration_catalog(consumer_id="miku")}
        search_ids = {item["id"] for item in registry.integration_catalog(consumer_id="search")}

        self.assertIn("search.global.v1", miku_ids)
        self.assertNotIn("music.search.documents.v1", miku_ids)
        self.assertIn("music.search.documents.v1", search_ids)
        self.assertNotIn("search.global.v1", search_ids)

    def test_snapshot_refresh_upserts_and_removes_deleted_documents(self):
        engine = create_engine("sqlite://")
        SearchDocumentIndex.__table__.create(engine)
        SearchSyncState.__table__.create(engine)
        SearchRefreshOutbox.__table__.create(engine)
        with Session(engine) as sync_session:
            session = AsyncSessionAdapter(sync_session)
            registry = SearchProviderRegistry()
            context = IntegrationContext(
                session=session,
                user=SimpleNamespace(id=1),
                registry=registry,
                consumer_id="miku",
            )

            first = asyncio.run(global_search(GlobalSearchRequest(query="zero escape"), context))
            self.assertEqual(["video-1"], [item.document_id for item in first.items])
            self.assertEqual("search.documents.v1", registry.last_contract)

            registry.documents = registry.documents[:1]
            asyncio.run(refresh_index(context, force=True))
            indexed = list(sync_session.scalars(select(SearchDocumentIndex)))
            self.assertEqual(["video-1"], [item.document_id for item in indexed])

            registry.fail = True
            with self.assertLogs("app.modules.search.integrations", level="ERROR"):
                warnings = asyncio.run(refresh_index(context, force=True))
            indexed = list(sync_session.scalars(select(SearchDocumentIndex)))
            self.assertEqual(["video_archiver search index is stale"], warnings)
            self.assertEqual(["video-1"], [item.document_id for item in indexed])

        engine.dispose()

    def test_source_mutation_enqueues_transactional_refresh(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        register_search_outbox()
        self.addCleanup(unregister_search_outbox)
        with Session(engine) as session:
            session.add(Song(title="Indexed later", audio_file_id="music/later.mp3"))
            session.flush()

            queued = list(session.scalars(select(SearchRefreshOutbox)))
            self.assertEqual(["music"], [item.source_module_id for item in queued])
            session.rollback()

            self.assertEqual([], list(session.scalars(select(SearchRefreshOutbox))))

            media = LibMedia(title="Indexed book", slug="indexed-book", media_type="novel")
            media.chapters.append(LibChapter(volume="1", number="1", volume_int=1, number_float=1))
            session.add(media)
            session.flush()

            queued = list(session.scalars(select(SearchRefreshOutbox)))
            self.assertEqual(["alllib"], [item.source_module_id for item in queued])
        engine.dispose()

    def test_search_routes_require_owner_authentication(self):
        routes = {
            (method, route.path): route
            for route in router.routes
            for method in (getattr(route, "methods", None) or set())
        }

        self.assertEqual(
            {("GET", "/api/search/status"), ("POST", "/api/search/reindex")},
            set(routes),
        )
        for route in routes.values():
            self.assertIn(get_current_user, {dependency.call for dependency in route.dependant.dependencies})

    def test_global_search_indexes_real_module_providers(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with Session(engine) as sync_session:
            sync_session.add_all(
                [
                    Song(title="Neon Song", author="Test Artist", audio_file_id="music/neon.mp3"),
                    ArchivedVideo(
                        id="zero-escape",
                        title="Zero Escape finale",
                        description="The final archived episode",
                        platform="youtube",
                        channel_name="Archive Channel",
                        duration=120,
                        resolution="1080p",
                        file_path="video_archiver/zero-escape.mp4",
                        status="completed",
                    ),
                    LibMedia(
                        title="Zero Escape novel",
                        slug="zero-escape",
                        media_type="novel",
                    ),
                    VaultItem(
                        entry_type="thought",
                        node_type="note",
                        title="Zero Escape notes",
                        content="Puzzle observations",
                    ),
                ]
            )
            sync_session.commit()
            registry = ModuleRegistry.discover()
            context = IntegrationContext(
                session=AsyncSessionAdapter(sync_session),
                user=SimpleNamespace(id=1),
                registry=registry,
                consumer_id="miku",
            )

            result = asyncio.run(
                registry.invoke_integration(
                    "search.global.v1",
                    {"query": "Zero Escape finale", "limit": 10},
                    context,
                )
            )

            self.assertEqual(
                {"alllib", "vault", "video_archiver"},
                {item["source_module_id"] for item in result["items"]},
            )
            self.assertEqual("video_archiver", result["items"][0]["source_module_id"])

        engine.dispose()


if __name__ == "__main__":
    unittest.main()
