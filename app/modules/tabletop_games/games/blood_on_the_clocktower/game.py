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
TEAM_KEYS = ("townsfolk", "outsider", "minion", "demon")
TEENSYVILLE_SCRIPTS = frozenset({"no_greater_joy", "over_the_river", "laissez_un_faire"})


def standard_distribution(player_count: int) -> dict[str, int]:
    if player_count not in PLAYER_DISTRIBUTION:
        raise ValueError("Для игры нужно от 5 до 15 игроков")
    return dict(zip(TEAM_KEYS, PLAYER_DISTRIBUTION[player_count], strict=True))


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    player_limit = int(config.get("player_limit", 10))
    if player_limit not in PLAYER_DISTRIBUTION:
        raise ValueError("Для игры нужно от 5 до 15 игроков")
    script = str(config.get("script", "trouble_brewing"))
    if script not in SCRIPT_ROLES:
        raise ValueError("Неизвестный сценарий")
    if script in TEENSYVILLE_SCRIPTS and player_limit > 6:
        raise ValueError("Сценарии Teensyville рассчитаны на 5–6 игроков")
    player_chat = str(config.get("player_chat", "private"))
    if player_chat not in {"private", "gm_only", "off"}:
        raise ValueError("Неизвестный режим сообщений")
    evil_info = str(config.get("evil_info", "standard"))
    if evil_info not in {"standard", "always", "never"}:
        raise ValueError("Неизвестный режим информации злой команды")
    try:
        demon_bluff_count = int(config.get("demon_bluff_count", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректное число ролей для легенды") from exc
    if not 0 <= demon_bluff_count <= 5:
        raise ValueError("Для легенды можно выбрать от 0 до 5 ролей")
    evil_code_word = " ".join(str(config.get("evil_code_word", "")).split())
    if len(evil_code_word) > 80:
        raise ValueError("Кодовое слово не должно быть длиннее 80 символов")
    manual_distribution = bool(config.get("manual_distribution", False))
    role_counts = standard_distribution(player_limit)
    if manual_distribution:
        try:
            role_counts = {team: int(config[f"{team}_count"]) for team in TEAM_KEYS}
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Укажите количество всех команд для ручного состава") from exc
        if any(count < 0 for count in role_counts.values()) or sum(role_counts.values()) != player_limit:
            raise ValueError("Ручной состав должен быть неотрицательным и равным числу игроков")
        if any(role_counts[team] > len(roles_by_team(script)[team]) for team in TEAM_KEYS):
            raise ValueError("В сценарии недостаточно ролей для выбранного ручного состава")
    player_information = str(config.get("player_information", "full"))
    if player_information not in {"full", "role_only", "roster_only"}:
        raise ValueError("Неизвестный режим информации игроков")
    return {
        "player_limit": player_limit,
        "script": script,
        "player_chat": player_chat,
        "allow_player_messages": player_chat == "private",
        "evil_info": evil_info,
        "demon_bluff_count": demon_bluff_count,
        "minion_bluffs": bool(config.get("minion_bluffs", False)),
        "evil_code_word": evil_code_word,
        "manual_distribution": manual_distribution,
        "role_counts": role_counts,
        "player_information": player_information,
        "show_storyteller_reference": bool(config.get("show_storyteller_reference", True)),
        "show_online_status": bool(config.get("show_online_status", True)),
        "reveal_roles_on_end": bool(config.get("reveal_roles_on_end", True)),
    }


def assign_roles(player_count: int, config: dict[str, Any]) -> list[RoleDefinition]:
    if player_count not in PLAYER_DISTRIBUTION:
        raise ValueError("Для старта нужно от 5 до 15 игроков")
    script = str(config.get("script", "trouble_brewing"))
    if script not in SCRIPT_ROLES:
        raise ValueError("Неизвестный сценарий")
    if script in TEENSYVILLE_SCRIPTS and player_count > 6:
        raise ValueError("Сценарии Teensyville рассчитаны на 5–6 игроков")
    team_roles = roles_by_team(script)
    role_counts = config.get("role_counts")
    if not isinstance(role_counts, dict):
        role_counts = standard_distribution(player_count)
    townsfolk = int(role_counts["townsfolk"])
    outsiders = int(role_counts["outsider"])
    minion_count = int(role_counts["minion"])
    demons = int(role_counts["demon"])
    available_minions = team_roles["minion"]
    if not config.get("manual_distribution", False) and outsiders + 2 > len(team_roles["outsider"]):
        available_minions = tuple(item for item in available_minions if item.id != "baron")
    selected_minions = random.sample(available_minions, minion_count)
    selected_demons = random.sample(team_roles["demon"], demons)
    outsider_delta = 0
    if not config.get("manual_distribution", False):
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
                ("no_greater_joy", "No Greater Joy"),
                ("over_the_river", "Over the River"),
                ("laissez_un_faire", "Laissez un Faire"),
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
            "name": "player_information",
            "type": "select",
            "label_ru": "Информация для игроков",
            "options": (
                ("full", "Роль и памятка сценария"),
                ("role_only", "Только своя роль"),
                ("roster_only", "Только список игроков (вариант)"),
            ),
            "default": "full",
        },
        {
            "name": "manual_distribution",
            "type": "boolean",
            "label_ru": "Настроить состав команд вручную (вариант)",
            "default": False,
        },
        *(
            {
                "name": f"{team}_count",
                "type": "number",
                "label_ru": label,
                "min": 0,
                "max": len(roles_by_team("trouble_brewing")[team]),
                "default": "",
                "manual_distribution": True,
            }
            for team, label in (
                ("townsfolk", "Горожане"),
                ("outsider", "Чужаки"),
                ("minion", "Приспешники"),
                ("demon", "Демоны"),
            )
        ),
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
            "name": "demon_bluff_count",
            "type": "select",
            "label_ru": "Ролей для легенды Демона",
            "options": (
                ("0", "Не показывать"),
                ("1", "1 роль"),
                ("2", "2 роли"),
                ("3", "3 роли (стандарт)"),
                ("4", "4 роли"),
                ("5", "5 ролей"),
            ),
            "default": "3",
        },
        {
            "name": "minion_bluffs",
            "type": "boolean",
            "label_ru": "Показывать легенду также приспешникам (вариант)",
            "default": False,
        },
        {
            "name": "evil_code_word",
            "type": "text",
            "label_ru": "Кодовое слово злой команды (необязательно)",
            "max_length": 80,
            "default": "",
        },
        {
            "name": "show_online_status",
            "type": "boolean",
            "label_ru": "Показывать игрокам статус онлайна",
            "default": True,
        },
        {
            "name": "show_storyteller_reference",
            "type": "boolean",
            "label_ru": "Показывать ведущему памятку сценария",
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
    role_catalog=tuple({role.id: role for roles in SCRIPT_ROLES.values() for role in roles}.values()),
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
            {
                "id": "no_greater_joy",
                "title": "No Greater Joy",
                "level": "Teensyville · первая игра",
                "description": "Простой следующий шаг после Trouble Brewing для 5–6 игроков.",
                "max_players": 6,
            },
            {
                "id": "over_the_river",
                "title": "Over the River",
                "level": "Teensyville · средний",
                "description": "Компактный сценарий для 5–6 игроков с меняющимися ролями и регистрацией.",
                "max_players": 6,
            },
            {
                "id": "laissez_un_faire",
                "title": "Laissez un Faire",
                "level": "Teensyville · сложный",
                "description": "Напряжённая пятидневная партия с Левиафаном для 5–6 игроков.",
                "max_players": 6,
            },
        ),
        "script_role_ids": {
            script: tuple(role.id for role in roles) for script, roles in SCRIPT_ROLES.items()
        },
    },
)
