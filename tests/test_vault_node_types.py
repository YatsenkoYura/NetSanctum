"""What a card's type is allowed to become.

`node_type` names the *shape* of a card — what opens when you click it, which
view renders it — and nothing about what is on it. That distinction is the
whole reason the column is structural rather than sealed: a locked vault must
still be able to lay its grid out, and the grid can only do that if it knows
what kind of tile it is painting.

So this file pins three things that a type system would otherwise be free to
break:

* the type stays structural, because a locked grid has to read it;
* the type never carries provenance, because anyone holding the database can
  read a structural column while the vault is locked;
* a type is the only thing that decides which view opens, so it cannot quietly
  acquire a second opinion from somewhere else.
"""

import importlib
import subprocess
import unittest
from pathlib import Path

from app.modules.vault import node_types
from app.modules.vault.sealing import SEALED_FIELDS


class StructuralColumnTests(unittest.TestCase):
    def test_the_type_is_not_sealed(self):
        self.assertNotIn(
            "node_type",
            SEALED_FIELDS,
            "node_type names the shape of a card, not its content — sealing it would "
            "leave a locked vault unable to lay out its own grid",
        )

    def test_the_shape_columns_are_all_structural(self):
        """Everything the grid needs to draw a tile, and nothing that describes it."""
        for field in ("node_type", "entry_type", "is_folder", "is_pinned", "is_archived"):
            self.assertNotIn(field, SEALED_FIELDS, field)


class NodeTypeTests(unittest.TestCase):
    def test_the_values_are_the_ones_the_column_actually_holds(self):
        """The enum is documentation that can be checked. Every value must be a
        string the database can already contain, or it describes a world that
        does not exist."""
        for member in node_types.NodeType:
            self.assertIsInstance(member.value, str)
            self.assertTrue(member.value)

    def test_every_type_names_the_view_that_opens_it(self):
        for member in node_types.NodeType:
            self.assertTrue(
                node_types.view_for(member),
                f"{member.value} has no view: a card that opens nothing is a dead card",
            )

    def test_view_for_accepts_the_plain_string_too(self):
        """Rows arrive as strings from the database, not as enum members."""
        self.assertEqual(node_types.view_for("note"), node_types.view_for(node_types.NodeType.NOTE))
        self.assertEqual("editor", node_types.view_for("note"))

    def test_view_for_falls_back_for_a_type_it_has_never_seen(self):
        """A row from a newer build, or a local type, must still open as
        something rather than dead-end the whole grid on one unknown tile."""
        self.assertTrue(node_types.view_for("a-type-from-the-future"))

    def test_nothing_in_the_enum_leaks_a_site(self):
        """A type is readable while the vault is locked, so it may not be named
        after where the card came from. Provenance belongs in the sealed payload
        or behind the alias — never in the structural column."""
        for member in node_types.NodeType:
            self.assertFalse(node_types.looks_like_provenance(member.value), member.value)

    def test_the_provenance_check_notices_a_type_named_after_its_source(self):
        for leaky in ("youtube_comment", "TikTokClip", "reddit_thread"):
            self.assertTrue(node_types.looks_like_provenance(leaky), leaky)
        for neutral in ("quote", "recipe", "clip", "note"):
            self.assertFalse(node_types.looks_like_provenance(neutral), neutral)


class RegistryTests(unittest.TestCase):
    """The single place a type may be declared."""

    def test_every_registered_type_is_a_real_enum_member(self):
        for name in node_types.registered_view_names():
            self.assertIn(name, {member.value for member in node_types.NodeType}, name)

    def test_the_registry_has_no_duplicate_entries(self):
        names = node_types.registered_view_names()
        self.assertEqual(len(names), len(set(names)), "a type declared twice is a dict that silently won")


class ViewMapTests(unittest.TestCase):
    """What the dashboard is told to open, and how it is told."""

    def test_the_map_covers_every_built_in_type(self):
        mapping = node_types.view_map()
        for member in node_types.NodeType:
            self.assertIn(member.value, mapping, member.value)
            self.assertEqual(node_types.view_for(member), mapping[member.value])

    def test_the_map_reaches_the_page_as_data_not_as_script(self):
        """The template escapes it with `|tojson`. A pre-dumped string marked
        `|safe` would let any value containing `</script>` end the block early,
        and the local directory is the one place a value can come from outside
        the repository."""
        template = Path("app/modules/vault/templates/vault_dashboard.html").read_text()
        self.assertIn("{{ vault_views | tojson }}", template)
        self.assertNotIn("{{ vault_views | safe }}", template)

    def test_the_map_reaches_both_render_paths_not_just_its_own_route(self):
        """This dashboard is rendered by two callers — its own route and the
        sharing module as a read-only page — so a value passed in one route's
        context simply does not exist on the other. That is not theoretical: it
        raised on the shared render and took the page down.

        So the table is published into the render context by the engine, from a
        dict the module fills at import time.
        """
        router = Path("app/modules/vault/router.py").read_text()
        module = Path("app/modules/vault/module.py").read_text()
        templates = Path("app/core/templates.py").read_text()
        self.assertNotIn("vault_views", router, "the route must not be the one that supplies it")
        self.assertIn("publish_view_map()", module)
        self.assertIn("context_processors", templates)

    def test_the_holder_module_cannot_cycle_with_the_registry(self):
        """`app.core.templates` imports the module registry, so a module
        importing it while discovery is importing that module deadlocks — and
        the module then drops out of the registry silently. The holder exists so
        that import is never needed."""
        import ast

        tree = ast.parse(Path("app/core/template_globals.py").read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        self.assertEqual({"typing"}, imported, f"the holder must stay dependency-free, got {imported}")

    def test_publishing_does_not_freeze_the_value_at_engine_build_time(self):
        """An engine built before a module is imported would otherwise capture
        an empty table, which is the same bug in a quieter form."""
        import app.core.templates as core_templates
        from app.core.template_globals import DEFERRED_GLOBALS

        self.assertIn(core_templates.deferred_globals, core_templates.templates.context_processors)
        self.assertIn("vault_views", DEFERRED_GLOBALS)
        self.assertEqual("sheet", core_templates.deferred_globals(None)["vault_views"]["table"])


class LocalTypesTests(unittest.TestCase):
    """The owner's own card types, kept out of the repository.

    The point of the directory is that adding a card type costs no committed
    change, so these tests write into it, prove the map grows, and clean up.
    """

    DIRECTORY = Path("app/modules/vault/local_types")

    def _write(self, name: str, body: str):
        self.DIRECTORY.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: (self.DIRECTORY / name).unlink(missing_ok=True))
        (self.DIRECTORY / name).write_text(body)

    def test_the_directory_is_not_tracked_by_git(self):
        """Otherwise "not in git" is a promise the .gitignore has to keep."""
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(self.DIRECTORY)],
            capture_output=True,
        )
        if ignored.returncode == 127:
            self.skipTest("git is not available")
        self.assertEqual(0, ignored.returncode, f"{self.DIRECTORY} is not ignored")

    def test_an_absent_directory_is_simply_no_local_types(self):
        """A fresh clone has no directory at all, and that must not be an error."""
        self.assertIsInstance(node_types.local_views(), dict)

    def test_a_local_type_reaches_the_map(self):
        self._write("probe_cards.py", 'VIEWS = {"probe": "editor"}\n')
        importlib.invalidate_caches()

        self.assertEqual("editor", node_types.view_map()["probe"])

    def test_a_broken_local_file_costs_its_cards_not_the_dashboard(self):
        self._write("broken_cards.py", "raise RuntimeError('boom')\n")
        importlib.invalidate_caches()

        with self.assertLogs("app.modules.vault.node_types", level="WARNING"):
            views = node_types.local_views()
        self.assertNotIn("broken", views)

    def test_a_file_without_views_is_skipped_rather_than_guessed_at(self):
        self._write("quiet_cards.py", "SOMETHING_ELSE = 1\n")
        importlib.invalidate_caches()

        with self.assertLogs("app.modules.vault.node_types", level="WARNING"):
            views = node_types.local_views()
        self.assertNotIn("quiet", views)


class CaptureKindTests(unittest.TestCase):
    """The kinds the extension may push, checked in one place.

    The closed vocabulary used to be a `Literal` inside the schema, which meant
    a new kind could not be added without editing the request model — and an
    unknown one had no defined answer at all until it reached the service.
    """

    def test_every_kind_says_which_card_type_it_makes(self):
        for name, spec in node_types.CAPTURE_KINDS.items():
            self.assertTrue(spec.node_type, name)
            self.assertIn(spec.node_type, node_types.VIEWS, f"{name} makes a type with no view")

    def test_a_kind_is_looked_up_by_its_plain_string(self):
        self.assertEqual(
            node_types.CAPTURE_KINDS["video"].node_type,
            node_types.capture_kind("video").node_type,
        )

    def test_an_unknown_kind_is_refused_by_name(self):
        """The refusal has to say what is accepted, or the extension's author
        is left guessing at a 422 that only reads 'validation error'."""
        with self.assertRaises(ValueError) as caught:
            node_types.capture_kind("a-kind-nobody-registered")
        message = str(caught.exception)
        for name in node_types.CAPTURE_KINDS:
            self.assertIn(name, message)

    def test_a_missing_kind_is_refused_rather_than_defaulted(self):
        """Silently storing a capture whose kind we did not recognise is how a
        card ends up with bytes nobody can place."""
        for empty in (None, "", "   "):
            with self.assertRaises(ValueError):
                node_types.capture_kind(empty)

    def test_only_the_archived_kind_is_archived(self):
        """Archiving is what makes a capture leave bytes behind with a worker
        that has no vault key, so it cannot become a property of every kind."""
        archived = [name for name, spec in node_types.CAPTURE_KINDS.items() if spec.archived]
        self.assertEqual(["video"], archived)

    def test_the_capture_schema_rejects_an_unknown_kind(self):
        from pydantic import ValidationError

        from app.modules.vault.schemas import VaultCaptureCreate

        with self.assertRaises(ValidationError):
            VaultCaptureCreate(kind="a-kind-nobody-registered", title="что-то")

    def test_the_capture_schema_still_accepts_every_registered_kind(self):
        from app.modules.vault.schemas import VaultCaptureCreate

        for name in node_types.CAPTURE_KINDS:
            kwargs = {"kind": name, "title": "что-то"}
            if name == "video":
                kwargs["video_url"] = "https://example.com/watch?v=1"
            else:
                kwargs["image"] = "data:image/png;base64,AAAA"
            self.assertEqual(name, VaultCaptureCreate(**kwargs).kind)


def test_a_local_type_named_after_a_site_is_warned_about_not_refused(self):
    """It is the owner's vault and their threat model — but node_type is
    readable while it is locked, so the name has to be said out loud once."""
    self._write("site_cards.py", 'VIEWS = {"youtube_comment": "editor"}\n')
    importlib.invalidate_caches()

    with self.assertLogs("app.modules.vault.node_types", level="WARNING") as seen:
        views = node_types.local_views()
    self.assertIn("youtube_comment", views)
    self.assertTrue(
        any("node_type is readable" in line for line in seen.output),
        seen.output,
    )


if __name__ == "__main__":
    unittest.main()
