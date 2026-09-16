from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    id: str
    name_ru: str
    name_en: str
    team: str
    ability_ru: str


@dataclass(frozen=True, slots=True)
class GameDefinition:
    id: str
    title_ru: str
    title_en: str
    description_ru: str
    min_players: int
    max_players: int
    accent: str
    config_schema: tuple[dict[str, Any], ...]
    validate_config: Callable[[dict[str, Any]], dict[str, Any]]
    assign_roles: Callable[[int, dict[str, Any]], list[RoleDefinition]]
    role_catalog: tuple[RoleDefinition, ...] = ()
    cover_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
