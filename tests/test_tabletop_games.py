import random
import unittest

from app.core.realtime import RealtimeHub
from app.modules.tabletop_games.games.blood_on_the_clocktower.game import (
    PLAYER_DISTRIBUTION,
    assign_roles,
    validate_config,
)
from app.modules.tabletop_games.registry import game_registry
from app.modules.tabletop_games.schemas import ParticipantEffect, ParticipantUpdate
from app.modules.tabletop_games.services import hash_player_token, player_cookie_name, room_channel


class TabletopGameRegistryTests(unittest.TestCase):
    def test_clocktower_is_discovered_from_its_own_game_package(self):
        game = game_registry.get("blood_on_the_clocktower")

        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(5, game.min_players)
        self.assertEqual(15, game.max_players)
        self.assertGreater(len(game.role_catalog), 70)

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

    def test_each_clocktower_script_has_a_complete_unique_role_set(self):
        game = game_registry.get("blood_on_the_clocktower")
        assert game is not None

        script_role_ids = game.metadata["script_role_ids"]
        for script, role_ids in script_role_ids.items():
            with self.subTest(script=script):
                roles = assign_roles(12, {"script": script})
                self.assertEqual(12, len(roles))
                self.assertEqual(12, len({role.id for role in roles}))
                self.assertLessEqual({role.id for role in roles}, set(role_ids))
                self.assertEqual(1, sum(role.team == "demon" for role in roles))

    def test_clocktower_keeps_game_specific_options(self):
        config = validate_config(
            {
                "player_limit": 12,
                "script": "sects_and_violets",
                "player_chat": "gm_only",
                "evil_info": "never",
                "show_online_status": False,
                "reveal_roles_on_end": False,
            }
        )

        self.assertEqual("sects_and_violets", config["script"])
        self.assertEqual("gm_only", config["player_chat"])
        self.assertEqual("never", config["evil_info"])
        self.assertFalse(config["show_online_status"])
        self.assertFalse(config["reveal_roles_on_end"])

    def test_clocktower_supports_a_manual_team_distribution(self):
        config = validate_config(
            {
                "player_limit": 10,
                "script": "trouble_brewing",
                "manual_distribution": True,
                "townsfolk_count": 6,
                "outsider_count": 1,
                "minion_count": 2,
                "demon_count": 1,
                "player_information": "roster_only",
            }
        )

        roles = assign_roles(10, config)
        self.assertEqual({"townsfolk": 6, "outsider": 1, "minion": 2, "demon": 1}, config["role_counts"])
        self.assertEqual(6, sum(role.team == "townsfolk" for role in roles))
        self.assertEqual(1, sum(role.team == "outsider" for role in roles))
        self.assertEqual(2, sum(role.team == "minion" for role in roles))
        self.assertEqual(1, sum(role.team == "demon" for role in roles))

    def test_clocktower_rejects_an_incomplete_manual_distribution(self):
        with self.assertRaises(ValueError):
            validate_config({"player_limit": 7, "manual_distribution": True, "townsfolk_count": 5})

    def test_clocktower_keeps_evil_information_options(self):
        config = validate_config(
            {
                "player_limit": 10,
                "demon_bluff_count": 5,
                "minion_bluffs": True,
                "evil_code_word": "  moon   is  red ",
            }
        )

        self.assertEqual(5, config["demon_bluff_count"])
        self.assertTrue(config["minion_bluffs"])
        self.assertEqual("moon is red", config["evil_code_word"])

    def test_each_script_exposes_roles_for_manual_storyteller_assignment(self):
        game = game_registry.get("blood_on_the_clocktower")
        assert game is not None

        for script, role_ids in game.metadata["script_role_ids"].items():
            with self.subTest(script=script):
                choices = [role for role in game.role_catalog if role.id in role_ids]
                self.assertEqual(set(role_ids), {role.id for role in choices})
                self.assertTrue(any(role.team == "demon" for role in choices))


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

    def test_storyteller_role_and_effect_payloads_are_bounded(self):
        self.assertEqual("imp", ParticipantUpdate(role_id="imp").role_id)
        self.assertEqual("Отравлен", ParticipantEffect(effect="Отравлен").effect)


if __name__ == "__main__":
    unittest.main()
