import random
import unittest

from app.core.realtime import RealtimeHub
from app.modules.tabletop_games.games.blood_on_the_clocktower.game import (
    PLAYER_DISTRIBUTION,
    assign_roles,
    validate_config,
)
from app.modules.tabletop_games.registry import game_registry
from app.modules.tabletop_games.services import hash_player_token, player_cookie_name, room_channel


class TabletopGameRegistryTests(unittest.TestCase):
    def test_clocktower_is_discovered_from_its_own_game_package(self):
        game = game_registry.get("blood_on_the_clocktower")

        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(5, game.min_players)
        self.assertEqual(15, game.max_players)
        self.assertGreater(len(game.role_catalog), 20)

    def test_clocktower_assigns_complete_unique_role_sets(self):
        random.seed(42)
        for player_count, base_distribution in PLAYER_DISTRIBUTION.items():
            with self.subTest(player_count=player_count):
                roles = assign_roles(player_count, {})
                role_ids = [role.id for role in roles]
                self.assertEqual(player_count, len(roles))
                self.assertEqual(player_count, len(set(role_ids)))
                self.assertEqual(1, sum(role.team == "demon" for role in roles))
                expected_townsfolk, expected_outsiders, expected_minions, _ = base_distribution
                if "baron" in role_ids:
                    expected_townsfolk -= 2
                    expected_outsiders += 2
                self.assertEqual(expected_townsfolk, sum(role.team == "townsfolk" for role in roles))
                self.assertEqual(expected_outsiders, sum(role.team == "outsider" for role in roles))
                self.assertEqual(expected_minions, sum(role.team == "minion" for role in roles))

    def test_clocktower_rejects_invalid_player_limits(self):
        with self.assertRaises(ValueError):
            validate_config({"player_limit": 4})


class TabletopSecurityTests(unittest.TestCase):
    def test_player_token_helpers_do_not_expose_raw_token(self):
        token = "private-player-token"

        self.assertNotEqual(token, hash_player_token(token))
        self.assertEqual(64, len(hash_player_token(token)))
        self.assertEqual("tabletop_abcd2345", player_cookie_name("ABCD2345"))

    def test_realtime_channels_are_namespaced_and_validated(self):
        channel = room_channel("123e4567-e89b-12d3-a456-426614174000")

        self.assertEqual(channel, RealtimeHub.validate_channel(channel))
        with self.assertRaises(ValueError):
            RealtimeHub.validate_channel("../room")


if __name__ == "__main__":
    unittest.main()
