import random
from typing import Any

from app.modules.tabletop_games.domain import GameDefinition, RoleDefinition
from app.modules.tabletop_games.games.blood_on_the_clocktower.roles import ROLES_BY_TEAM, TROUBLE_BREWING

PLAYER_DISTRIBUTION = {
    5: (3, 0, 1, 1),
    6: (3, 1, 1, 1),
    7: (5, 0, 1, 1),
    8: (5, 1, 1, 1),
    9: (5, 2, 1, 1),
    10: (7, 0, 2, 1),
    11: (7, 1, 2, 1),
    12: (7, 2, 2, 1),
    13: (9, 0, 3, 1),
    14: (9, 1, 3, 1),
    15: (9, 2, 3, 1),
}


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    player_limit = int(config.get("player_limit", 10))
    if player_limit not in PLAYER_DISTRIBUTION:
        raise ValueError("Для сценария «Сбой в системе» нужно от 5 до 15 игроков")
    return {
        "player_limit": player_limit,
        "script": "trouble_brewing",
        "allow_player_messages": bool(config.get("allow_player_messages", True)),
    }


def assign_roles(player_count: int, config: dict[str, Any]) -> list[RoleDefinition]:
    if player_count not in PLAYER_DISTRIBUTION:
        raise ValueError("Для старта нужно от 5 до 15 игроков")
    townsfolk, outsiders, minion_count, demons = PLAYER_DISTRIBUTION[player_count]
    selected_minions = random.sample(ROLES_BY_TEAM["minion"], minion_count)
    if any(item.id == "baron" for item in selected_minions):
        townsfolk -= 2
        outsiders += 2
    selected = [
        *random.sample(ROLES_BY_TEAM["townsfolk"], townsfolk),
        *random.sample(ROLES_BY_TEAM["outsider"], outsiders),
        *selected_minions,
        *random.sample(ROLES_BY_TEAM["demon"], demons),
    ]
    random.shuffle(selected)
    return selected


GAME = GameDefinition(
    id="blood_on_the_clocktower",
    title_ru="Кровь на часовой башне",
    title_en="Blood on the Clocktower",
    description_ru="Социальная дедукция с живым ведущим, тайными ролями и гримуаром.",
    min_players=5,
    max_players=15,
    accent="#b91c1c",
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
            "options": (("trouble_brewing", "Сбой в системе"),),
            "default": "trouble_brewing",
        },
        {
            "name": "allow_player_messages",
            "type": "boolean",
            "label_ru": "Разрешить личные сообщения игроков",
            "default": True,
        },
    ),
    validate_config=validate_config,
    assign_roles=assign_roles,
    role_catalog=TROUBLE_BREWING,
)
