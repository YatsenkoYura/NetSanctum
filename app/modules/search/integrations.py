import asyncio
import logging
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from uuid import uuid4

from sqlalchemy import delete, select, text

from app.contracts.search_documents_v1 import SearchDocumentsRequest, SearchDocumentsResult
from app.contracts.search_query_v1 import GlobalSearchHit, GlobalSearchRequest, GlobalSearchResult
from app.core.module_types import (
    IntegrationContext,
    IntegrationRejectedError,
    IntegrationServiceError,
    IntegrationUnavailableError,
)
from app.modules.search.models import SearchDocumentIndex, SearchRefreshOutbox, SearchSyncState

DOCUMENTS_CONTRACT = "search.documents.v1"
SYNC_TTL = timedelta(minutes=5)
FAILED_SYNC_RETRY_DELAY = timedelta(minutes=1)
MAX_PROVIDER_DOCUMENTS = 10_000
MAX_CANDIDATES = 5_000
logger = logging.getLogger(__name__)
refresh_lock = asyncio.Lock()

POSTGRES_SEARCH_SQL_BASE = """
SELECT
    d.source_module_id,
    d.source_integration_id,
    d.document_id,
    d.entity_type,
    d.title,
    d.subtitle,
    d.body,
    d.open_path,
    d.playable,
    d.readable,
    d.normalized_title,
    d.search_text,
    (CASE
        WHEN d.normalized_title = :query_norm THEN 2.0
        WHEN d.normalized_title ILIKE :query_prefix THEN 1.8
        WHEN (d.search_vector @@ (websearch_to_tsquery('russian', :query_raw) || websearch_to_tsquery('english', :query_raw)))
            THEN 1.0 + ts_rank_cd('{0.1, 0.2, 0.4, 1.0}', d.search_vector, (websearch_to_tsquery('russian', :query_raw) || websearch_to_tsquery('english', :query_raw)), 32)
        ELSE GREATEST(
            similarity(d.normalized_title, :query_norm),
            similarity(d.keywords_text, :query_norm),
            word_similarity(:query_norm, d.normalized_title) * 0.9
        )
    END) AS calculated_score
FROM search_documents d
WHERE
    (:module_ids_empty OR d.source_module_id = ANY(:module_ids))
    AND (:entity_types_empty OR d.entity_type = ANY(:entity_types))
    AND (
        (d.search_vector @@ (websearch_to_tsquery('russian', :query_raw) || websearch_to_tsquery('english', :query_raw)))
        OR similarity(d.normalized_title, :query_norm) >= 0.35
        OR similarity(d.keywords_text, :query_norm) >= 0.35
        OR word_similarity(:query_norm, d.normalized_title) >= 0.60
    )
__REQUIRED_FILTER__
ORDER BY calculated_score DESC, d.source_module_id, d.normalized_title, d.document_id
LIMIT :limit;
"""

POSTGRES_SEARCH_SQL = text(POSTGRES_SEARCH_SQL_BASE.replace("__REQUIRED_FILTER__", ""))


def _postgres_search_statement(required_count: int):
    # Generic exact-token MUST filter: every required term must appear as a
    # whole token in the normalized search_text. No language knowledge here,
    # just token equality, so numbers/identifiers/codes all behave the same.
    clauses = "".join(
        f"    AND :req_{index} = ANY(string_to_array(d.search_text, ' '))\n"
        for index in range(max(0, required_count))
    )
    return text(POSTGRES_SEARCH_SQL_BASE.replace("__REQUIRED_FILTER__", clauses))


def normalize_search_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.findall(r"[\w]+", normalized, re.UNICODE))


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _sync_provider(record, spec, context: IntegrationContext) -> int:
    generation = str(uuid4())
    offset = 0
    count = 0
    while True:
        result = await context.registry.invoke_integration(
            spec.id,
            SearchDocumentsRequest(offset=offset).model_dump(mode="json"),
            IntegrationContext(
                session=context.session,
                user=context.user,
                registry=context.registry,
                consumer_id="search",
            ),
        )
        page = SearchDocumentsResult.model_validate(result)
        if page.module_id != record.id:
            raise IntegrationRejectedError("Search provider returned the wrong module ID")
        for document in page.documents:
            existing = await context.session.scalar(
                select(SearchDocumentIndex).where(
                    SearchDocumentIndex.source_integration_id == spec.id,
                    SearchDocumentIndex.document_id == document.document_id,
                )
            )
            keywords_text = " ".join(document.keywords)
            values = {
                "source_module_id": record.id,
                "source_integration_id": spec.id,
                "document_id": document.document_id,
                "entity_type": document.entity_type,
                "title": document.title,
                "subtitle": document.subtitle,
                "body": document.body,
                "keywords_text": keywords_text,
                "normalized_title": normalize_search_text(document.title),
                "search_text": normalize_search_text(
                    " ".join(
                        value
                        for value in (document.title, document.subtitle, document.body, keywords_text)
                        if value
                    )
                ),
                "open_path": document.open_path,
                "playable": document.playable,
                "readable": document.readable,
                "source_updated_at": document.updated_at,
                "indexed_at": datetime.now(UTC),
                "generation": generation,
            }
            if existing:
                for key, value in values.items():
                    setattr(existing, key, value)
            else:
                context.session.add(SearchDocumentIndex(**values))
            count += 1
        await context.session.flush()
        if page.next_offset is None:
            break
        if page.next_offset <= offset or count >= MAX_PROVIDER_DOCUMENTS:
            raise IntegrationRejectedError("Search provider returned invalid or excessive pagination")
        offset = page.next_offset
    await context.session.execute(
        delete(SearchDocumentIndex).where(
            SearchDocumentIndex.source_integration_id == spec.id,
            SearchDocumentIndex.generation != generation,
        )
    )
    state = await context.session.get(SearchSyncState, spec.id)
    if not state:
        state = SearchSyncState(source_integration_id=spec.id, source_module_id=record.id)
        context.session.add(state)
    state.generation = generation
    state.last_completed_at = datetime.now(UTC)
    state.last_error = None
    state.document_count = count
    return count


async def refresh_index(context: IntegrationContext, *, force: bool = False) -> list[str]:
    warnings = []
    now = datetime.now(UTC)
    providers = context.registry.integration_providers(DOCUMENTS_CONTRACT)
    active_ids = {spec.id for _, spec in providers}
    dirty_modules = set(
        await context.session.scalars(select(SearchRefreshOutbox.source_module_id).distinct())
    )
    for record, spec in providers:
        state = await context.session.get(SearchSyncState, spec.id)
        completed = _aware(state.last_completed_at) if state else None
        attempted = _aware(state.last_attempted_at) if state else None
        if (
            not force
            and record.id not in dirty_modules
            and completed
            and not state.last_error
            and now - completed < SYNC_TTL
        ):
            continue
        if (
            not force
            and state
            and state.last_error
            and attempted
            and now - attempted < FAILED_SYNC_RETRY_DELAY
        ):
            warnings.append(f"{record.id} search index is stale")
            continue
        if not state:
            state = SearchSyncState(source_integration_id=spec.id, source_module_id=record.id)
            context.session.add(state)
        state.last_attempted_at = now
        try:
            async with context.session.begin_nested():
                await _sync_provider(record, spec, context)
                await context.session.execute(
                    delete(SearchRefreshOutbox).where(SearchRefreshOutbox.source_module_id == record.id)
                )
        except Exception:
            logger.exception("Search index refresh failed for provider %s", spec.id)
            state = await context.session.get(SearchSyncState, spec.id)
            state.last_error = "provider refresh failed"
            warnings.append(f"{record.id} search index is stale")
    stale_sources = await context.session.scalars(select(SearchSyncState.source_integration_id))
    for integration_id in set(stale_sources) - active_ids:
        await context.session.execute(
            delete(SearchDocumentIndex).where(SearchDocumentIndex.source_integration_id == integration_id)
        )
        state = await context.session.get(SearchSyncState, integration_id)
        if state:
            await context.session.delete(state)
    await context.session.flush()
    return warnings


SEARCH_STOP_WORDS = {
    # Russian
    "и",
    "в",
    "во",
    "не",
    "что",
    "он",
    "на",
    "я",
    "с",
    "со",
    "как",
    "а",
    "то",
    "все",
    "она",
    "так",
    "его",
    "но",
    "да",
    "ты",
    "к",
    "у",
    "же",
    "вы",
    "за",
    "бы",
    "по",
    "только",
    "ее",
    "мне",
    "было",
    "вот",
    "от",
    "меня",
    "еще",
    "нет",
    "о",
    "из",
    "ему",
    "теперь",
    "когда",
    "даже",
    "ну",
    "вдруг",
    "ли",
    "если",
    "уже",
    "или",
    "ни",
    "быть",
    "был",
    "него",
    "до",
    "вас",
    "нибудь",
    "какой",
    "какая",
    "какое",
    "какие",
    "чей",
    "чья",
    "чье",
    "чьи",
    "для",
    "под",
    "над",
    "про",
    "без",
    "через",
    "при",
    "об",
    # English
    "a",
    "an",
    "the",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "up",
    "about",
    "into",
    "over",
    "after",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "shall",
    "should",
    "can",
    "could",
    "may",
    "might",
    "must",
    "it",
    "its",
    "this",
    "that",
}


def _token_match(token: str, word: str) -> float:
    if token == word:
        return 1.0
    len_t = len(token)
    len_w = len(word)
    if len_t >= 3 and len_w >= 3:
        if token.startswith(word) or word.startswith(token):
            return max(0.85, 0.95 - (abs(len_t - len_w) * 0.05))
        if len_t >= 4 and len_w >= 4 and token[:4] == word[:4]:
            sim = SequenceMatcher(None, token, word).ratio()
            if sim >= 0.7:
                return 0.85
    return 0.0


def _contains_required_terms(search_text: str, required_terms: list[str]) -> bool:
    words = set(search_text.split())
    return all(normalize_search_text(term) in words for term in required_terms)


def _score(document: SearchDocumentIndex, query: str, tokens: list[str]) -> float:
    title = document.normalized_title
    if title == query:
        return 1.0
    if title.startswith(query):
        return 0.96
    if query in title:
        return 0.92

    title_words = title.split()
    title_initials = "".join(w[0] for w in title_words if w)

    if len(tokens) == 1 and len(tokens[0]) >= 3 and tokens[0] == title_initials:
        return 0.92

    subtitle_words = normalize_search_text(document.subtitle).split() if document.subtitle else []
    keywords_words = normalize_search_text(document.keywords_text).split() if document.keywords_text else []
    body_words = normalize_search_text(document.body).split() if document.body else []

    content_tokens = [t for t in tokens if t not in SEARCH_STOP_WORDS and len(t) >= 2]
    if not content_tokens:
        content_tokens = tokens

    matched_count = 0
    primary_matched_count = 0
    token_scores: list[float] = []

    for token in content_tokens:
        m_title = max((_token_match(token, w) for w in title_words), default=0.0)
        if len(token) >= 3 and token == title_initials:
            m_title = max(m_title, 0.95)

        m_kw = max((_token_match(token, w) for w in keywords_words), default=0.0)
        m_sub = max((_token_match(token, w) for w in subtitle_words), default=0.0)
        m_body = max((_token_match(token, w) for w in body_words), default=0.0)

        t_score = max(m_title * 1.0, m_kw * 0.88, m_sub * 0.65, m_body * 0.25)
        token_scores.append(t_score)

        if t_score >= 0.20:
            matched_count += 1
        if max(m_title, m_kw, m_sub) >= 0.5:
            primary_matched_count += 1

    total_tokens = len(content_tokens)
    coverage = matched_count / total_tokens if total_tokens else 0.0
    primary_coverage = primary_matched_count / total_tokens if total_tokens else 0.0

    if coverage < 1.0:
        if primary_matched_count == 0:
            return 0.0
        if coverage < 0.5 and primary_coverage < 0.5:
            return round(coverage * 0.4 * primary_coverage, 4)

    avg_token_score = sum(token_scores) / total_tokens if total_tokens else 0.0
    title_sim = SequenceMatcher(None, query, title).ratio()

    if coverage >= 1.0:
        if primary_coverage > 0:
            score = max(0.70 + (0.22 * primary_coverage), avg_token_score, title_sim * 0.85)
        else:
            score = max(0.40, avg_token_score)
    else:
        penalty = (coverage**0.8) * (0.7 + 0.3 * primary_coverage)
        score = (avg_token_score * 0.85 + title_sim * 0.15) * penalty

    return min(round(score, 4), 1.0)


async def global_search(
    request: GlobalSearchRequest,
    context: IntegrationContext,
) -> GlobalSearchResult:
    if context.consumer_id != "miku":
        raise IntegrationUnavailableError("Global search is currently scoped to MIKU")
    async with refresh_lock:
        warnings = await refresh_index(context)

    queries = list(dict.fromkeys([request.query, *request.alternate_queries]))
    normalized_queries = [normalize_search_text(query) for query in queries]
    if not any(normalized_queries) and not request.required_terms:
        # Nothing searchable was asked for: an empty result keeps the agent loop
        # alive so it can ask for clarification instead of failing the turn.
        return GlobalSearchResult(items=[], warnings=[*warnings, "The query had no searchable terms."])

    get_bind_fn = getattr(context.session, "get_bind", None)
    bind = get_bind_fn() if callable(get_bind_fn) else None
    dialect = getattr(bind, "dialect", None)
    is_postgres = getattr(dialect, "name", None) == "postgresql"

    required_terms = [
        normalized for term in request.required_terms if (normalized := normalize_search_text(term))
    ]
    if is_postgres:
        statement = _postgres_search_statement(len(required_terms))
        merged: dict[tuple[str, str], GlobalSearchHit] = {}
        for raw_query, normalized_query in zip(queries, normalized_queries, strict=True):
            params: dict[str, object] = {
                "query_raw": raw_query,
                "query_norm": normalized_query,
                "query_prefix": f"{normalized_query}%",
                "module_ids_empty": not bool(request.module_ids),
                "module_ids": list(request.module_ids or []),
                "entity_types_empty": not bool(request.entity_types),
                "entity_types": list(request.entity_types or []),
                "limit": request.limit,
            }
            params.update({f"req_{index}": term for index, term in enumerate(required_terms)})
            rows = (await context.session.execute(statement, params)).fetchall()
            for row in rows:
                if not _contains_required_terms(row.search_text, required_terms):
                    continue
                score = min(
                    float(row.calculated_score) / 2.0
                    if row.calculated_score > 1.0
                    else float(row.calculated_score),
                    1.0,
                )
                key = (row.source_integration_id, row.document_id)
                existing = merged.get(key)
                if existing and existing.score >= score:
                    continue
                merged[key] = GlobalSearchHit(
                    source_module_id=row.source_module_id,
                    source_integration_id=row.source_integration_id,
                    document_id=row.document_id,
                    entity_type=row.entity_type,
                    title=row.title,
                    subtitle=row.subtitle,
                    summary=(row.body[:300] if row.body else None),
                    open_path=row.open_path,
                    playable=row.playable,
                    readable=row.readable,
                    score=round(score, 4),
                )
        hits = sorted(
            merged.values(),
            key=lambda item: (-item.score, item.source_module_id, item.title, item.document_id),
        )[: request.limit]
        return GlobalSearchResult(items=hits, stale=bool(warnings), warnings=warnings)

    statement = select(SearchDocumentIndex)
    if request.module_ids:
        statement = statement.where(SearchDocumentIndex.source_module_id.in_(request.module_ids))
    if request.entity_types:
        statement = statement.where(SearchDocumentIndex.entity_type.in_(request.entity_types))
    documents = list((await context.session.scalars(statement.limit(MAX_CANDIDATES))).all())
    if request.required_terms:
        documents = [
            document
            for document in documents
            if _contains_required_terms(document.search_text, request.required_terms)
        ]
    if warnings and not documents:
        raise IntegrationServiceError("The private search index is unavailable")
    ranked = sorted(
        (
            (
                max(
                    _score(document, normalized_query, normalized_query.split())
                    for normalized_query in normalized_queries
                ),
                document,
            )
            for document in documents
        ),
        key=lambda item: (
            -item[0],
            item[1].source_module_id,
            item[1].normalized_title,
            item[1].document_id,
        ),
    )
    hits = []
    for score, document in ranked:
        if score < 0.20:
            continue
        hits.append(
            GlobalSearchHit(
                source_module_id=document.source_module_id,
                source_integration_id=document.source_integration_id,
                document_id=document.document_id,
                entity_type=document.entity_type,
                title=document.title,
                subtitle=document.subtitle,
                summary=(document.body[:300] if document.body else None),
                open_path=document.open_path,
                playable=document.playable,
                readable=document.readable,
                score=round(score, 4),
            )
        )
        if len(hits) >= request.limit:
            break
    return GlobalSearchResult(items=hits, stale=bool(warnings), warnings=warnings)
