from app.modules.tabletop_games.domain import RoleDefinition


def role(role_id: str, name_ru: str, name_en: str, team: str, ability_ru: str) -> RoleDefinition:
    return RoleDefinition(role_id, name_ru, name_en, team, ability_ru)


CLASSIC = (
    role(
        "detective",
        "Детектив",
        "Detective",
        "townsfolk",
        "Каждую ночь проверяйте игрока: ведущий сообщает, состоит ли он в мафии.",
    ),
    role("doctor", "Доктор", "Doctor", "townsfolk", "Каждую ночь защищайте одного игрока от убийства мафии."),
    role(
        "vigilante",
        "Мститель",
        "Vigilante",
        "townsfolk",
        "Один раз за игру ночью можете застрелить выбранного игрока.",
    ),
    role(
        "bodyguard",
        "Телохранитель",
        "Bodyguard",
        "townsfolk",
        "Каждую ночь охраняйте игрока. Если его атакуют, вы можете погибнуть вместо него.",
    ),
    role(
        "journalist",
        "Журналист",
        "Journalist",
        "townsfolk",
        "Каждую ночь выберите двух игроков и узнайте, есть ли среди них хотя бы один мафиози.",
    ),
    role(
        "priest",
        "Священник",
        "Priest",
        "townsfolk",
        "Один раз за игру можете вернуть ночью погибшего мирного игрока.",
    ),
    role(
        "psychologist",
        "Психолог",
        "Psychologist",
        "townsfolk",
        "Каждую ночь выберите игрока: он не может использовать ночную способность.",
    ),
    role(
        "sheriff",
        "Шериф",
        "Sheriff",
        "townsfolk",
        "Каждую ночь проверяйте игрока: ведущий сообщает, опасен ли он для города.",
    ),
    role(
        "informant", "Осведомитель", "Informant", "townsfolk", "В начале игры узнаёте одного мирного игрока."
    ),
    role(
        "tracker",
        "Следопыт",
        "Tracker",
        "townsfolk",
        "Каждую ночь узнаёте, посещал ли выбранный игрок кого-либо.",
    ),
    role("mafioso", "Мафиози", "Mafioso", "minion", "Знаете союзников и участвуете в ночном выборе жертвы."),
    role(
        "consigliere",
        "Консильери",
        "Consigliere",
        "minion",
        "Каждую ночь проверяйте игрока и узнавайте его роль.",
    ),
    role(
        "silencer",
        "Заглушитель",
        "Silencer",
        "minion",
        "Каждую ночь выберите игрока: на следующий день он не может говорить.",
    ),
    role(
        "blackmailer",
        "Шантажист",
        "Blackmailer",
        "minion",
        "Каждую ночь выберите игрока: на следующий день он не может голосовать.",
    ),
    role(
        "roleblocker",
        "Блокировщик",
        "Roleblocker",
        "minion",
        "Каждую ночь лишайте выбранного игрока его ночной способности.",
    ),
    role("don", "Дон", "Don", "demon", "Руководите мафией. Каждую ночь выбирайте жертву вместе с мафиози."),
)

NOIR = (
    *CLASSIC[:6],
    role(
        "hacker",
        "Хакер",
        "Hacker",
        "townsfolk",
        "Каждую ночь можете узнать, использовал ли игрок свою способность.",
    ),
    role(
        "medium",
        "Медиум",
        "Medium",
        "townsfolk",
        "Каждую ночь можете задать ведущему один вопрос о погибшем игроке.",
    ),
    role(
        "judge",
        "Судья",
        "Judge",
        "townsfolk",
        "Один раз за игру ваш голос при дневном голосовании считается двойным.",
    ),
    role(
        "deputy",
        "Помощник шерифа",
        "Deputy",
        "townsfolk",
        "Если Шериф погибает, вы узнаёте, кто был Шерифом, и получаете его способность.",
    ),
    *CLASSIC[10:],
)

CHAOS = (
    *CLASSIC[:5],
    *NOIR[6:10],
    role(
        "sniper",
        "Снайпер",
        "Sniper",
        "townsfolk",
        "Один раз за игру ночью выберите игрока: он погибает, если не защищён.",
    ),
    *CLASSIC[10:],
)

SCRIPT_ROLES = {"classic": CLASSIC, "noir": NOIR, "chaos": CHAOS}


def roles_by_team(script: str) -> dict[str, tuple[RoleDefinition, ...]]:
    return {
        team: tuple(role for role in SCRIPT_ROLES[script] if role.team == team)
        for team in ("townsfolk", "outsider", "minion", "demon")
    }
