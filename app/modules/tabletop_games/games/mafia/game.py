import random
from typing import Any

from app.modules.tabletop_games.domain import GameDefinition, RoleDefinition
from app.modules.tabletop_games.games.mafia.roles import SCRIPT_ROLES, roles_by_team

PLAYER_DISTRIBUTION = {
    5: (3, 0, 1, 1),
    6: (4, 0, 1, 1),
    7: (5, 0, 1, 1),
    8: (6, 0, 1, 1),
    9: (6, 0, 2, 1),
    10: (7, 0, 2, 1),
    11: (8, 0, 2, 1),
    12: (8, 0, 3, 1),
    13: (9, 0, 3, 1),
    14: (10, 0, 3, 1),
    15: (10, 0, 4, 1),
}
TEAM_KEYS = ("townsfolk", "outsider", "minion", "demon")


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    player_limit = int(config.get("player_limit", 10))
    if player_limit not in PLAYER_DISTRIBUTION:
        raise ValueError("Для Мафии нужно от 5 до 15 игроков")
    script = str(config.get("script", "classic"))
    if script not in SCRIPT_ROLES:
        raise ValueError("Неизвестный сценарий Мафии")
    player_chat = str(config.get("player_chat", "private"))
    if player_chat not in {"private", "gm_only", "off"}:
        raise ValueError("Неизвестный режим сообщений")
    return {
        "player_limit": player_limit,
        "script": script,
        "player_chat": player_chat,
        "allow_player_messages": player_chat == "private",
        "evil_info": str(config.get("evil_info", "standard")),
        "demon_bluff_count": 0,
        "minion_bluffs": False,
        "evil_code_word": "",
        "player_information": str(config.get("player_information", "role_only")),
        "show_storyteller_reference": bool(config.get("show_storyteller_reference", True)),
        "show_online_status": bool(config.get("show_online_status", True)),
        "reveal_roles_on_end": bool(config.get("reveal_roles_on_end", True)),
        "role_counts": dict(zip(TEAM_KEYS, PLAYER_DISTRIBUTION[player_limit], strict=True)),
    }


def assign_roles(player_count: int, config: dict[str, Any]) -> list[RoleDefinition]:
    if player_count not in PLAYER_DISTRIBUTION:
        raise ValueError("Для Мафии нужно от 5 до 15 игроков")
    script = str(config.get("script", "classic"))
    if script not in SCRIPT_ROLES:
        raise ValueError("Неизвестный сценарий Мафии")
    role_counts = config.get(
        "role_counts", dict(zip(TEAM_KEYS, PLAYER_DISTRIBUTION[player_count], strict=True))
    )
    by_team = roles_by_team(script)
    selected = [
        *random.sample(by_team["townsfolk"], int(role_counts["townsfolk"])),
        *random.sample(by_team["minion"], int(role_counts["minion"])),
        *random.sample(by_team["demon"], int(role_counts["demon"])),
    ]
    random.shuffle(selected)
    return selected


GAME = GameDefinition(
    id="mafia",
    title_ru="Мафия: Ночной город",
    title_en="Mafia: Night City",
    description_ru="Ночная социальная дедукция с расширенными ролями и ведущим.",
    min_players=5,
    max_players=15,
    accent="#f43f5e",
    config_schema=(
        {
            "name": "player_limit",
            "type": "number",
            "label_ru": "Максимум игроков",
            "min": 5,
            "max": 15,
            "default": 10,
        },
        {
            "name": "script",
            "type": "select",
            "label_ru": "Сценарий",
            "options": (("classic", "Классика"), ("noir", "Нуар"), ("chaos", "Хаос")),
            "default": "classic",
        },
        {
            "name": "player_chat",
            "type": "select",
            "label_ru": "Сообщения игроков",
            "options": (
                ("private", "Ведущему и друг другу"),
                ("gm_only", "Только ведущему"),
                ("off", "Отключить"),
            ),
            "default": "private",
        },
        {
            "name": "player_information",
            "type": "select",
            "label_ru": "Информация для игроков",
            "options": (
                ("full", "Роль и памятка"),
                ("role_only", "Только своя роль"),
                ("roster_only", "Только список игроков"),
            ),
            "default": "role_only",
        },
        {
            "name": "show_online_status",
            "type": "boolean",
            "label_ru": "Показывать статус онлайна",
            "default": True,
        },
        {
            "name": "show_storyteller_reference",
            "type": "boolean",
            "label_ru": "Показывать ведущему памятку",
            "default": True,
        },
        {
            "name": "reveal_roles_on_end",
            "type": "boolean",
            "label_ru": "Раскрыть роли после завершения",
            "default": True,
        },
    ),
    validate_config=validate_config,
    assign_roles=assign_roles,
    role_catalog=tuple({role.id: role for roles in SCRIPT_ROLES.values() for role in roles}.values()),
    cover_url="/static/tabletop-mafia.svg",
    metadata={
        "scenarios": (
            {
                "id": "classic",
                "title": "Классика",
                "level": "Первая партия",
                "description": "Детектив, Доктор и привычная мафия.",
            },
            {
                "id": "noir",
                "title": "Нуар",
                "level": "Расширенный",
                "description": "Расследования, связи и давление на город.",
            },
            {
                "id": "chaos",
                "title": "Хаос",
                "level": "Продвинутый",
                "description": "Больше активных ролей и опасных ночей.",
            },
        ),
        "script_role_ids": {
            script: tuple(role.id for role in roles) for script, roles in SCRIPT_ROLES.items()
        },
    },
)
