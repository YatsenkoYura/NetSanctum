import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.core.realtime import RealtimeHub
from app.modules.tabletop_games.games.blood_on_the_clocktower.game import (
    PLAYER_DISTRIBUTION,
    assign_roles,
    validate_config,
)
from app.modules.tabletop_games.games.mafia.game import (
    PLAYER_DISTRIBUTION as MAFIA_PLAYER_DISTRIBUTION,
    assign_roles as assign_mafia_roles,
    validate_config as validate_mafia_config,
)
from app.modules.tabletop_games.registry import game_registry
from app.modules.tabletop_games.router import (
    TabletopOperator,
    require_room,
    require_tabletop_operator,
)
from app.modules.tabletop_games.schemas import ParticipantEffect, ParticipantUpdate
from app.modules.tabletop_games.services import (
    create_room,
    hash_player_token,
    player_cookie_name,
    room_channel,
)


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
        scenario_by_id = {scenario["id"]: scenario for scenario in game.metadata["scenarios"]}
        for script, role_ids in script_role_ids.items():
            with self.subTest(script=script):
                player_count = min(12, scenario_by_id[script].get("max_players", 12))
                roles = assign_roles(player_count, {"script": script})
                self.assertEqual(player_count, len(roles))
                self.assertEqual(player_count, len({role.id for role in roles}))
                self.assertLessEqual({role.id for role in roles}, set(role_ids))
                self.assertEqual(1, sum(role.team == "demon" for role in roles))

    def test_clocktower_exposes_official_teensyville_scripts(self):
        game = game_registry.get("blood_on_the_clocktower")
        assert game is not None

        expected_scripts = {
            "no_greater_joy": {
                "clockmaker",
                "investigator",
                "empath",
                "chambermaid",
                "artist",
                "sage",
                "drunk",
                "klutz",
                "scarlet_woman",
                "baron",
                "imp",
            },
            "over_the_river": {
                "grandmother",
                "clockmaker",
                "innkeeper",
                "snake_charmer",
                "professor",
                "slayer",
                "lunatic",
                "recluse",
                "godfather",
                "spy",
                "imp",
            },
            "laissez_un_faire": {
                "balloonist",
                "savant",
                "amnesiac",
                "fisherman",
                "artist",
                "cannibal",
                "mutant",
                "lunatic",
                "widow",
                "goblin",
                "leviathan",
            },
        }
        scenario_by_id = {scenario["id"]: scenario for scenario in game.metadata["scenarios"]}
        for script, expected_role_ids in expected_scripts.items():
            with self.subTest(script=script):
                self.assertEqual(expected_role_ids, set(game.metadata["script_role_ids"][script]))
                self.assertEqual(6, scenario_by_id[script]["max_players"])

    def test_clocktower_limits_teensyville_to_six_players(self):
        config = validate_config({"player_limit": 6, "script": "no_greater_joy"})

        self.assertEqual(6, config["player_limit"])
        with self.assertRaises(ValueError):
            validate_config({"player_limit": 7, "script": "no_greater_joy"})
        with self.assertRaises(ValueError):
            assign_roles(7, {"script": "no_greater_joy"})

    def test_six_player_no_greater_joy_excludes_baron(self):
        for _ in range(20):
            roles = assign_roles(6, {"script": "no_greater_joy"})
            self.assertNotIn("baron", {role.id for role in roles})

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

    def test_mafia_is_discovered_with_a_large_role_catalog(self):
        game = game_registry.get("mafia")

        self.assertIsNotNone(game)
        assert game is not None
        self.assertEqual(5, game.min_players)
        self.assertEqual(15, game.max_players)
        self.assertGreaterEqual(len(game.role_catalog), 20)

    def test_mafia_assigns_complete_unique_role_sets_for_every_scenario(self):
        game = game_registry.get("mafia")
        assert game is not None

        for script, role_ids in game.metadata["script_role_ids"].items():
            for player_count, distribution in MAFIA_PLAYER_DISTRIBUTION.items():
                with self.subTest(script=script, player_count=player_count):
                    roles = assign_mafia_roles(player_count, {"script": script})
                    self.assertEqual(player_count, len(roles))
                    self.assertEqual(player_count, len({role.id for role in roles}))
                    self.assertLessEqual({role.id for role in roles}, set(role_ids))
                    self.assertEqual(distribution[0], sum(role.team == "townsfolk" for role in roles))
                    self.assertEqual(distribution[2], sum(role.team == "minion" for role in roles))
                    self.assertEqual(1, sum(role.team == "demon" for role in roles))

    def test_mafia_rejects_unknown_script(self):
        with self.assertRaises(ValueError):
            validate_mafia_config({"script": "unknown"})


class TabletopSharedOperatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_operator_is_accepted_without_owner_session(self):
        owner_error = HTTPException(status_code=401, detail="Invalid session")
        with (
            patch(
                "app.modules.tabletop_games.router.get_current_user",
                AsyncMock(side_effect=owner_error),
            ),
            patch(
                "app.modules.tabletop_games.router.get_operator_share_id",
                AsyncMock(return_value="share-id"),
            ),
        ):
            operator = await require_tabletop_operator(SimpleNamespace())

        self.assertEqual("share-id", operator.share_id)
        self.assertIsNone(operator.user)

    async def test_shared_operator_can_only_access_rooms_from_its_link(self):
        own_room = SimpleNamespace(operator_share_id="share-id")
        other_room = SimpleNamespace(operator_share_id="other-share")
        db = AsyncMock()
        operator = TabletopOperator(share_id="share-id")

        with patch(
            "app.modules.tabletop_games.router.get_room",
            AsyncMock(side_effect=[own_room, other_room]),
        ):
            self.assertIs(own_room, await require_room(db, "own-room", operator))
            with self.assertRaises(HTTPException) as raised:
                await require_room(db, "other-room", operator)

        self.assertEqual(404, raised.exception.status_code)

    async def test_shared_operator_id_is_stored_on_created_room(self):
        db = MagicMock()
        db.scalar = AsyncMock(return_value=None)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        game = game_registry.get("blood_on_the_clocktower")
        assert game is not None

        room = await create_room(
            db,
            game,
            "Shared game",
            {"player_limit": 5},
            operator_share_id="share-id",
        )

        self.assertEqual("share-id", room.operator_share_id)


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
