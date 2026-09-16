import importlib
import pkgutil

from app.modules.tabletop_games.domain import GameDefinition


class GameRegistry:
    def __init__(self) -> None:
        self._games: dict[str, GameDefinition] = {}

    @classmethod
    def discover(cls) -> "GameRegistry":
        registry = cls()
        import app.modules.tabletop_games.games as games_package

        for _finder, package, is_package in pkgutil.iter_modules(
            games_package.__path__, prefix="app.modules.tabletop_games.games."
        ):
            if not is_package:
                continue
            game = importlib.import_module(f"{package}.game").GAME
            if not isinstance(game, GameDefinition):
                raise TypeError(f"{package}.game must expose a GameDefinition")
            if game.id in registry._games:
                raise ValueError(f"Duplicate tabletop game id: {game.id}")
            registry._games[game.id] = game
        return registry

    def all(self) -> tuple[GameDefinition, ...]:
        return tuple(sorted(self._games.values(), key=lambda game: game.title_ru))

    def get(self, game_id: str) -> GameDefinition | None:
        return self._games.get(game_id)


game_registry = GameRegistry.discover()
