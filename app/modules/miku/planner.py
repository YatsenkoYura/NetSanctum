from typing import cast

from app.modules.miku.schemas import MikuCommand, MikuDecision

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
    "repeat": "repeat",
    "повтори": "repeat",
    "discover": "discover",
    "youtube": "discover",
    "ютуб": "discover",
    "open": "open",
    "открой": "open",
    "play": "play",
    "играй": "play",
    "archive": "archive",
    "архивируй": "archive",
    "note": "note",
    "заметка": "note",
    "запиши": "note",
    "bookmark": "bookmark",
    "закладка": "bookmark",
}


class MikuQueryError(ValueError):
    pass


def plan_with_rules(message: str) -> MikuDecision:
    parts = message.split(maxsplit=1)
    command = COMMAND_ALIASES.get(parts[0].casefold())
    if command is None:
        raise MikuQueryError("Unknown command. Use help to list available commands.")
    argument = parts[1].strip() if len(parts) == 2 else ""
    if command in {"find", "discover", "open", "play", "archive", "note", "bookmark"} and not argument:
        raise MikuQueryError(f"The {command} command requires an argument.")
    if command in {"help", "sources", "repeat"} and argument:
        raise MikuQueryError(f"The {command} command does not accept arguments.")
    return MikuDecision(command=cast(MikuCommand, command), argument=argument)
