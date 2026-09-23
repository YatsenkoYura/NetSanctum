from app.modules.miku.schemas import MikuDecision

COMMAND_ALIASES = {
    "help": "help",
    "помощь": "help",
    "sources": "sources",
    "источники": "sources",
    "list": "list",
    "список": "list",
    "find": "find",
    "search": "find",
    "найди": "find",
    "поиск": "find",
}


class MikuQueryError(ValueError):
    pass


def plan_with_rules(message: str) -> MikuDecision:
    parts = message.split(maxsplit=1)
    command = COMMAND_ALIASES.get(parts[0].casefold())
    if command is None:
        raise MikuQueryError("Unknown command. Use help to list available commands.")
    argument = parts[1].strip() if len(parts) == 2 else ""
    if command == "find" and not argument:
        raise MikuQueryError("The find command requires search text.")
    if command in {"help", "sources"} and argument:
        raise MikuQueryError(f"The {command} command does not accept arguments.")
    return MikuDecision(command=command, argument=argument)
