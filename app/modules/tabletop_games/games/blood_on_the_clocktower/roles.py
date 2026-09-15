from app.modules.tabletop_games.domain import RoleDefinition


def role(role_id: str, name_ru: str, name_en: str, team: str, ability_ru: str) -> RoleDefinition:
    return RoleDefinition(role_id, name_ru, name_en, team, ability_ru)


TROUBLE_BREWING = (
    role(
        "washerwoman",
        "Прачка",
        "Washerwoman",
        "townsfolk",
        "В первую ночь вы узнаете, что один из двух игроков является указанным горожанином.",
    ),
    role(
        "librarian",
        "Библиотекарь",
        "Librarian",
        "townsfolk",
        "В первую ночь вы узнаете, что один из двух игроков является указанным чужаком, либо что чужаков нет.",
    ),
    role(
        "investigator",
        "Следователь",
        "Investigator",
        "townsfolk",
        "В первую ночь вы узнаете, что один из двух игроков является указанным приспешником.",
    ),
    role(
        "chef",
        "Повар",
        "Chef",
        "townsfolk",
        "В первую ночь вы узнаете число пар злых игроков, сидящих рядом.",
    ),
    role(
        "empath",
        "Эмпат",
        "Empath",
        "townsfolk",
        "Каждую ночь вы узнаете, сколько из двух живых соседей являются злыми.",
    ),
    role(
        "fortune_teller",
        "Гадалка",
        "Fortune Teller",
        "townsfolk",
        "Каждую ночь выберите двух игроков: вы узнаете, есть ли среди них Демон. Есть красная селёдка.",
    ),
    role(
        "undertaker",
        "Гробовщик",
        "Undertaker",
        "townsfolk",
        "Каждую ночь, кроме первой, вы узнаете роль казнённого сегодня игрока.",
    ),
    role(
        "monk",
        "Монах",
        "Monk",
        "townsfolk",
        "Каждую ночь, кроме первой, выберите другого игрока: этой ночью Демон не может причинить ему вред.",
    ),
    role(
        "ravenkeeper",
        "Хранитель воронов",
        "Ravenkeeper",
        "townsfolk",
        "Если вы умрёте ночью, вас разбудят: выберите игрока и узнайте его роль.",
    ),
    role(
        "virgin", "Девственница", "Virgin", "townsfolk", "Первый выдвинувший вас горожанин немедленно казнён."
    ),
    role(
        "slayer",
        "Истребитель",
        "Slayer",
        "townsfolk",
        "Один раз за игру публично выберите игрока: если это Демон, он умирает.",
    ),
    role("soldier", "Солдат", "Soldier", "townsfolk", "Демон не может вас убить."),
    role(
        "mayor",
        "Мэр",
        "Mayor",
        "townsfolk",
        "Если в финале трое живых не казнят никого, добро побеждает. Ночная смерть может перейти на другого.",
    ),
    role(
        "butler",
        "Дворецкий",
        "Butler",
        "outsider",
        "Каждую ночь выберите хозяина. Днём голосуйте только если голосует он.",
    ),
    role(
        "drunk",
        "Пьяница",
        "Drunk",
        "outsider",
        "Вы не знаете, что вы Пьяница. Вы думаете, что вы горожанин, но ваша способность не работает.",
    ),
    role(
        "recluse",
        "Затворник",
        "Recluse",
        "outsider",
        "Вы можете определяться как злой, приспешник или Демон даже после смерти.",
    ),
    role("saint", "Святой", "Saint", "outsider", "Если вас казнят, ваша команда проигрывает."),
    role(
        "poisoner",
        "Отравитель",
        "Poisoner",
        "minion",
        "Каждую ночь выберите игрока: его способность отравлена этой ночью и завтра днём.",
    ),
    role(
        "spy",
        "Шпион",
        "Spy",
        "minion",
        "Каждую ночь вы видите гримуар и можете определяться как добрый или горожанин.",
    ),
    role(
        "scarlet_woman",
        "Алая женщина",
        "Scarlet Woman",
        "minion",
        "Если при пяти и более живых Демон умирает, вы становитесь Демоном.",
    ),
    role("baron", "Барон", "Baron", "minion", "В игре на двух чужаков больше и на двух горожан меньше."),
    role(
        "imp",
        "Бес",
        "Imp",
        "demon",
        "Каждую ночь, кроме первой, выберите игрока: он умирает. Убейте себя, чтобы приспешник стал Бесом.",
    ),
)

ROLES_BY_TEAM = {
    team: tuple(item for item in TROUBLE_BREWING if item.team == team)
    for team in ("townsfolk", "outsider", "minion", "demon")
}
