import re

from pydantic import BaseModel, Field, field_validator

CONTRACT_ID = "search.query.v1"


class GlobalSearchRequest(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Search terms, titles, keywords, authors, or aliases (e.g., 're:zero', 'булки кефир', 'death note').",
    )
    limit: int = Field(default=10, ge=1, le=20)
    alternate_queries: list[str] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Optional alternate names, translations, spellings, or expanded abbreviations for the same target. "
            "Each entry is searched independently and results are merged."
        ),
    )
    required_terms: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Exact standalone terms that every result must contain, such as a requested number or identifier. "
            "Keep broader title words in query."
        ),
    )
    module_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Optional source module IDs. Omit when the request does not identify a source.",
    )
    entity_types: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Optional entity types advertised by indexed source modules. Omit when uncertain.",
    )

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Search query must not be blank")
        return value

    @field_validator("alternate_queries")
    @classmethod
    def normalize_alternate_queries(cls, values: list[str]):
        normalized = [" ".join(value.split()) for value in values]
        if any(not value or len(value) > 200 for value in normalized):
            raise ValueError("Alternate search queries must contain 1 to 200 characters")
        return list(dict.fromkeys(normalized))

    @field_validator("required_terms")
    @classmethod
    def normalize_required_terms(cls, values: list[str]):
        normalized = [normalize for item in values if (normalize := " ".join(item.casefold().split()))]
        if any(len(value) > 50 or len(value.split()) != 1 for value in normalized):
            raise ValueError("Required search terms must be single tokens up to 50 characters")
        return list(dict.fromkeys(normalized))

    @field_validator("module_ids", "entity_types")
    @classmethod
    def validate_filters(cls, values: list[str]):
        normalized = list(dict.fromkeys(value.strip().casefold() for value in values if value.strip()))
        if any(not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) for value in normalized):
            raise ValueError("Search filters contain an invalid identifier")
        return normalized


class GlobalSearchHit(BaseModel):
    source_module_id: str
    source_integration_id: str
    document_id: str
    entity_type: str
    title: str
    subtitle: str | None = None
    summary: str | None = None
    open_path: str | None = None
    playable: bool = False
    readable: bool = False
    score: float = Field(ge=0, le=1)


class GlobalSearchResult(BaseModel):
    items: list[GlobalSearchHit] = Field(default_factory=list, max_length=20)
    stale: bool = False
    warnings: list[str] = Field(default_factory=list, max_length=20)
