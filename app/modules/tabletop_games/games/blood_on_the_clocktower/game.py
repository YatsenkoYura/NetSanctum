import random
from typing import Any

from app.modules.tabletop_games.domain import GameDefinition, RoleDefinition
from app.modules.tabletop_games.games.blood_on_the_clocktower.roles import SCRIPT_ROLES, roles_by_team

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
        raise ValueError("Для игры нужно от 5 до 15 игроков")
    script = str(config.get("script", "trouble_brewing"))
    if script not in SCRIPT_ROLES:
        raise ValueError("Неизвестный сценарий")
    player_chat = str(config.get("player_chat", "private"))
    if player_chat not in {"private", "gm_only", "off"}:
        raise ValueError("Неизвестный режим сообщений")
    evil_info = str(config.get("evil_info", "standard"))
    if evil_info not in {"standard", "always", "never"}:
        raise ValueError("Неизвестный режим информации злой команды")
    return {
        "player_limit": player_limit,
        "script": script,
        "player_chat": player_chat,
        "allow_player_messages": player_chat == "private",
        "evil_info": evil_info,
        "show_online_status": bool(config.get("show_online_status", True)),
        "reveal_roles_on_end": bool(config.get("reveal_roles_on_end", True)),
    }


def assign_roles(player_count: int, config: dict[str, Any]) -> list[RoleDefinition]:
    if player_count not in PLAYER_DISTRIBUTION:
        raise ValueError("Для старта нужно от 5 до 15 игроков")
    script = str(config.get("script", "trouble_brewing"))
    if script not in SCRIPT_ROLES:
        raise ValueError("Неизвестный сценарий")
    team_roles = roles_by_team(script)
    townsfolk, outsiders, minion_count, demons = PLAYER_DISTRIBUTION[player_count]
    selected_minions = random.sample(team_roles["minion"], minion_count)
    selected_demons = random.sample(team_roles["demon"], demons)
    outsider_delta = 2 if any(item.id == "baron" for item in selected_minions) else 0
    if any(item.id == "fang_gu" for item in selected_demons):
        outsider_delta += 1
    if any(item.id == "vigormortis" for item in selected_demons):
        outsider_delta -= 1
    if any(item.id == "godfather" for item in selected_minions):
        outsider_delta += 1 if outsiders == 0 else -1
    adjusted_outsiders = max(0, min(len(team_roles["outsider"]), outsiders + outsider_delta))
    townsfolk -= adjusted_outsiders - outsiders
    selected = [
        *random.sample(team_roles["townsfolk"], townsfolk),
        *random.sample(team_roles["outsider"], adjusted_outsiders),
        *selected_minions,
        *selected_demons,
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
    accent="#2dd4bf",
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
            "options": (
                ("trouble_brewing", "Trouble Brewing"),
                ("bad_moon_rising", "Bad Moon Rising"),
                ("sects_and_violets", "Sects & Violets"),
            ),
            "default": "trouble_brewing",
        },
        {
            "name": "player_chat",
            "type": "select",
            "label_ru": "Сообщения игроков",
            "options": (
                ("private", "Ведущему и друг другу"),
                ("gm_only", "Только ведущему"),
                ("off", "Отключить отправку"),
            ),
            "default": "private",
        },
        {
            "name": "evil_info",
            "type": "select",
            "label_ru": "Информация злой команды",
            "options": (
                ("standard", "По стандартным правилам"),
                ("always", "Всегда показывать"),
                ("never", "Не показывать"),
            ),
            "default": "standard",
        },
        {
            "name": "show_online_status",
            "type": "boolean",
            "label_ru": "Показывать игрокам статус онлайна",
            "default": True,
        },
        {
            "name": "reveal_roles_on_end",
            "type": "boolean",
            "label_ru": "Раскрыть все роли после завершения",
            "default": True,
        },
    ),
    validate_config=validate_config,
    assign_roles=assign_roles,
    role_catalog=tuple(role for roles in SCRIPT_ROLES.values() for role in roles),
    cover_url="/static/tabletop-blood-clocktower.svg",
    metadata={
        "scenarios": (
            {
                "id": "trouble_brewing",
                "title": "Trouble Brewing",
                "level": "Первая игра",
                "description": "Прозрачная логика, надёжная информация и базовые механики.",
            },
            {
                "id": "bad_moon_rising",
                "title": "Bad Moon Rising",
                "level": "Опытные игроки",
                "description": "Много смертей, защиты, пьянства и сложных ночных решений.",
            },
            {
                "id": "sects_and_violets",
                "title": "Sects & Violets",
                "level": "Продвинутый",
                "description": "Меняющиеся роли, безумие и противоречивая информация.",
            },
        ),
        "script_role_ids": {
            script: tuple(role.id for role in roles) for script, roles in SCRIPT_ROLES.items()
        },
    },
)
