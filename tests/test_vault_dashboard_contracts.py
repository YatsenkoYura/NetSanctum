"""The Vault dashboard's reading order and manual order, as contracts.

These are not browser tests — there is no DOM here. They pin the things that
made the previous round of Vault bugs possible: a feature that lives only in a
template, with nothing to notice when a hook stops being called, or when two
gestures on the same tile start fighting over the same drop.

Each test reads the template as text and asserts on the wiring, because the
wiring *is* the feature. The behaviour behind it is covered in
`test_vault_space_order.py`.
"""

import re
import shutil
import unittest
from pathlib import Path

TEMPLATE = Path("app/modules/vault/templates/vault_dashboard.html").read_text()


def script_blocks() -> list[str]:
    return re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", TEMPLATE, re.S)


class ReadingOrderContractTests(unittest.TestCase):
    def test_the_four_views_each_publish_the_strip_state(self):
        """A view that forgets to sync leaves the strips on top of the grid."""
        for name in ("openEditorView", "openSheetView", "openWhiteboardView", "openMediaView"):
            body = TEMPLATE.split(f"function {name}(item) {{", 1)[1].split("\n}", 1)[0]
            self.assertIn("syncEdgeStrips();", body, name)

    def test_closing_a_view_hides_the_strips(self):
        # Every close funnels through hideViews, so that is the one place to check.
        self.assertIn("syncEdgeStrips();", TEMPLATE.split("function hideViews() {", 1)[1].split("\n}", 1)[0])

    def test_the_arrow_keys_page_between_notes(self):
        keydown = TEMPLATE.split("function __vaultBindReadingOrder() {", 1)[1]
        self.assertIn("e.key === 'ArrowLeft'", keydown)
        self.assertIn("e.key === 'ArrowRight'", keydown)

    def test_the_strips_exist_for_touch_and_carry_labels(self):
        self.assertIn('id="vault-edge-strips"', TEMPLATE)
        # Invisible is fine; unlabelled is not.
        for edge in ('data-vault-edge="-1"', 'data-vault-edge="1"'):
            self.assertIn(edge, TEMPLATE)
        self.assertEqual(2, TEMPLATE.count('class="vault-edge vault-edge-'))

    def test_the_strips_only_take_taps_on_a_touch_pointer(self):
        """On a mouse they would eat clicks over the margins for nothing."""
        self.assertIn("vault-edges-touch .vault-edge", TEMPLATE)
        self.assertIn("(pointer: coarse)", TEMPLATE)

    def test_typing_in_a_field_is_not_taken_as_paging(self):
        keydown = TEMPLATE.split("function __vaultBindReadingOrder() {", 1)[1]
        self.assertIn("input, textarea, [contenteditable=", keydown)

    def test_the_neighbour_is_found_from_the_view_own_id_field(self):
        """Reading the id from the view is what keeps the two in step."""
        self.assertIn("vaultActiveItemId", TEMPLATE)
        for field in ("editor-id", "media-id", "sheet-id", "wb-id"):
            self.assertIn(field, TEMPLATE)


class ManualOrderContractTests(unittest.TestCase):
    def test_tiles_are_draggable_and_the_drop_goes_to_the_move_endpoint(self):
        self.assertIn("/api/vault/items/move", TEMPLATE)
        self.assertIn('data-tile-id="${itemId}" draggable="true"', TEMPLATE)

    def test_a_drop_is_sent_as_neighbours_not_as_a_number(self):
        body = TEMPLATE.split("async function vaultCommitReorder() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("before_id", body)
        self.assertIn("after_id", body)

    def test_the_grip_keeps_the_stack_gesture_to_itself(self):
        """The grip means "build a stack"; the tile body means "reorder"."""
        reorder = TEMPLATE.split("function __vaultBindReorder() {", 1)[1]
        self.assertIn("if (e.target.closest && e.target.closest('[data-grip]')) return;", reorder)

    def test_stacking_does_not_answer_a_reorder_drop(self):
        """Both end on a tile. Without this every reorder ends in a false alarm."""
        stack_drop = TEMPLATE.split("function __vaultBindCardStacks() {", 1)[1].split(
            "grid.addEventListener('drop'", 1
        )[1]
        self.assertIn("vaultReorder.fromId !== null", stack_drop.split("\n  });", 1)[0])

    def test_the_root_drop_ignores_a_card_being_reordered(self):
        root_drop = TEMPLATE.split("function __vaultAttachRootDrop() {", 1)[1]
        self.assertIn("vaultReorder.fromId !== null", root_drop)

    def test_order_is_refused_where_it_cannot_mean_anything(self):
        for refusal in ("readOnlyMode", "packageMode", "!currentCollectionId"):
            self.assertIn(
                refusal, TEMPLATE.split("function vaultReorderRefusal() {", 1)[1].split("\n}", 1)[0]
            )

    def test_a_drop_line_is_shown_while_dragging(self):
        self.assertIn("vault-drop-line", TEMPLATE)
        self.assertIn("vaultShowDropMarker", TEMPLATE)


class SpaceTreeContractTests(unittest.TestCase):
    def test_the_sidebar_renders_a_tree_with_foldable_branches(self):
        self.assertIn("vaultSidebarRows", TEMPLATE)
        self.assertIn("vaultCollapsedSpaces", TEMPLATE)
        self.assertIn("data-depth", TEMPLATE)

    def test_a_space_shows_the_spaces_nested_under_it(self):
        self.assertIn('id="vault-child-spaces"', TEMPLATE)
        self.assertIn("renderChildSpaces", TEMPLATE)

    def test_nesting_is_its_own_gesture_not_the_merge_one(self):
        """Merging empties a space and deletes it; nesting keeps both."""
        self.assertIn("/api/vault/collections/move", TEMPLATE)
        self.assertIn("openNestMenu", TEMPLATE)
        self.assertIn("vaultDescendantIds", TEMPLATE)

    def test_switching_space_refreshes_the_nested_tiles(self):
        self.assertIn(
            "renderChildSpaces();",
            TEMPLATE.split("async function selectWorkspace(id, name) {", 1)[1].split("\n}", 1)[0],
        )

    def test_sealed_spaces_are_not_offered_as_nesting_targets(self):
        menu = TEMPLATE.split("function openNestMenu(event, id) {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (record.is_encrypted) return;", menu)


class SidebarRunsTests(unittest.TestCase):
    """The sidebar rows, built by the real code rather than described by it.

    Three rounds of bugs came out of this file and none were visible to a text
    contract: code pasted into the calendar's closure, rows rendered as
    `ws-item-undefined` because the records behind them carried no id, and a third
    flex child that pushed every label to the middle of its row. So the rows are
    built here, by executing the template's own functions against a stub DOM.

    Skipped where node is absent, which is where the authoritative run happens.
    """

    TEMPLATE_PATH = "app/modules/vault/templates/vault_dashboard.html"
    FIXTURE = "tests/fixtures/vault_sidebar_dom.mjs"

    def setUp(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed in this environment")
        self.node = node

    def _build(self):
        import json
        import subprocess

        result = subprocess.run([self.node, self.FIXTURE, self.TEMPLATE_PATH], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr[:500])
        return json.loads(result.stdout)

    def test_every_row_gets_an_id_the_drag_code_can_parse(self):
        rows = self._build()["rows"]
        self.assertTrue(rows)
        for row in rows:
            self.assertRegex(row["id"], r"^ws-item-\d+$", row["id"])
            self.assertIsInstance(row["parsedId"], int)

    def test_every_row_is_clickable_and_switches_to_its_own_space(self):
        data = self._build()
        clicked = [entry["id"] for entry in data["selected"] if entry["id"] is not None]
        self.assertEqual([row["parsedId"] for row in data["rows"]], clicked)

    def test_a_nested_space_is_rendered_one_level_deep(self):
        depths = {row["parsedId"]: row["depth"] for row in self._build()["rows"]}
        self.assertEqual("0", depths[1])
        self.assertEqual("1", depths[4], "the space nested under 2 must render indented")

    def test_only_a_space_with_children_gets_a_twisty(self):
        rows = {row["parsedId"]: row for row in self._build()["rows"]}
        self.assertTrue(rows[2]["hasTwisty"], "2 has a child, so it needs one")
        self.assertFalse(rows[1]["hasTwisty"], "1 is a leaf and must not offer a fold")
        self.assertTrue(rows[1]["hasTwistySpacer"], "a leaf still needs the indent")

    def test_the_label_takes_the_free_space_so_it_stays_next_to_the_twisty(self):
        """`space-between` with a third child centres the label instead."""
        for row in self._build()["rows"]:
            self.assertIn("flex-1", row["labelClasses"])


class FolderGestureContractTests(unittest.TestCase):
    """Dragging a space nests it; dissolving a folder is a separate, marked move.

    The drag used to merge, which emptied the dragged space and deleted it — a
    destructive result hidden behind a gesture that looks like filing a folder.
    Merging survives in the ⋯ menu, behind a confirm.
    """

    def test_the_drag_path_nests_and_never_merges(self):
        body = TEMPLATE.split("async function vaultDropWorkspace(", 1)[1].split("\n}", 1)[0]
        self.assertIn("/api/vault/collections/move", body)
        self.assertNotIn("/api/vault/collections/merge", body)

    def test_merging_is_reachable_only_through_the_menu(self):
        merge = TEMPLATE.split("async function vaultMergeInto(", 1)[1].split("\n}", 1)[0]
        self.assertIn("/api/vault/collections/merge", merge)
        self.assertIn("confirm(", merge, "merging destroys a workspace and must ask")
        # The only caller is the move menu; the definition itself does not count.
        callers = [
            line
            for line in TEMPLATE.split("\n")
            if "vaultMergeInto(" in line and not line.startswith("async function")
        ]
        self.assertEqual(1, len(callers), callers)
        menu = TEMPLATE.split("async function vaultMoveTo(", 1)[1].split("\n}", 1)[0]
        self.assertIn("vaultMergeInto(fromId, toId)", menu)

    def test_a_nested_space_carries_the_cross_that_dissolves_it(self):
        row = TEMPLATE.split("function vaultSidebarRow(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("collection.parent_id !== null", row, "only a folder can be dissolved")
        self.assertIn("dissolveSpace(collection)", row)

    def test_dissolving_goes_to_its_own_endpoint_and_says_what_moves(self):
        body = TEMPLATE.split("async function dissolveSpace(", 1)[1].split("\n}", 1)[0]
        self.assertIn("/dissolve", body)
        self.assertIn("confirm(", body)
        self.assertIn("done.spaces", body)
        self.assertIn("done.cards", body)

    def test_the_tile_of_a_nested_space_carries_the_cross_too(self):
        render = TEMPLATE.split("function renderChildSpaces(", 1)[1].split("\n}", 1)[0]
        self.assertIn("space-tile-dissolve", render)
        self.assertIn("dissolveSpace(child)", render)

    def test_a_space_cannot_be_dropped_inside_its_own_branch(self):
        """Refused on the server too, but finding out from a toast after the drop is worse."""
        body = TEMPLATE.split("function vaultDropTargetValid(", 1)[1].split("\n}", 1)[0]
        self.assertIn("vaultDescendantIds(fromId)", body)


class CreateSpaceInsideASpaceTests(unittest.TestCase):
    """A space can be created directly inside another one.

    The endpoint has taken `parent_id` since the tree landed, but the create form
    never sent it: nesting was only possible for spaces that already existed, by
    dragging them or picking a parent in the ⇥ menu. So there was no way to make
    a folder where you actually were.
    """

    def test_the_create_menu_offers_a_folder_inside_a_space(self):
        self.assertIn('id="vault-create-folder-option"', TEMPLATE)
        self.assertIn("openWorkspaceModal(currentCollectionId)", TEMPLATE)

    def test_the_form_sends_the_parent_it_was_opened_with(self):
        body = TEMPLATE.split("async function submitWorkspace() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("payload.parent_id = parentId", body)
        self.assertIn("vaultNewSpaceParentId", body)

    def test_the_folder_option_is_hidden_at_the_top_level(self):
        body = TEMPLATE.split("function syncFolderOption() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("currentCollectionId === null", body)
        boot = TEMPLATE.split("function __vaultBoot() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("syncFolderOption()", boot)

    def test_a_sealed_space_cannot_be_created_as_a_folder(self):
        body = TEMPLATE.split("async function submitWorkspace() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (sealed && parentId !== null)", body)

    def test_creating_a_space_no_longer_reloads_the_page(self):
        """The sidebar is built in JS now; a reload also threw away where you were."""
        body = TEMPLATE.split("async function submitWorkspace() {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("window.location.reload()", body)
        self.assertIn("loadVaultSummary()", body)

    def test_a_sealed_space_stays_a_root_on_the_server_too(self):
        # The rule is not only in the browser: the endpoint refuses it, so a
        # hand-written request cannot get around it.
        service = Path("app/modules/vault/services.py").read_text()
        self.assertIn("Зашифрованное пространство всегда остаётся на верхнем уровне", service)
        schema = Path("app/modules/vault/schemas.py").read_text()
        self.assertIn("parent_id: int | None = Field(default=None, ge=1)", schema)


class ScopeContractTests(unittest.TestCase):
    """A function the dashboard calls must live where the call can see it.

    This is here because of a bug that every other test in this file happily
    walked past: the reading-order and reorder code was pasted inside the mini
    calendar's `(function vcalBoot() { … })()`, so every symbol it declared was
    local to the calendar. The page threw `ReferenceError: … is not defined` from
    `__vaultBoot`, which meant the sidebar never loaded at all — while the text
    contracts below all passed, because text cannot see scope.
    """

    CALLER = "__vaultBoot"
    # Declaration forms, not the names: the call site inside __vaultBoot always
    # contains the bare name, so searching for it would find the call and pass
    # even when the declaration is unreachable.
    CALLED_FROM_BOOT = (
        "function __vaultBindReorder(",
        "function __vaultBindReadingOrder(",
        "function syncEdgeStrips(",
        "var vaultReorder",
    )

    def test_everything_boot_calls_is_defined_in_the_same_script_block(self):
        blocks = script_blocks()
        caller_blocks = [i for i, block in enumerate(blocks) if self.CALLER in block]
        self.assertEqual(1, len(caller_blocks), f"{self.CALLER} must be declared in one block only")
        home = blocks[caller_blocks[0]]

        for declaration in self.CALLED_FROM_BOOT:
            self.assertIn(declaration, home, f"{declaration} is not in the same block as {self.CALLER}")

    def test_the_block_that_runs_boot_does_not_hide_them_in_a_closure(self):
        """A declaration inside an IIFE is invisible to the code above it.

        Counted per line and only for real IIFEs — a bare `function () {` is a
        callback like `.then(function () {`, which closes again and hides nothing.
        """
        blocks = script_blocks()
        home = blocks[next(i for i, b in enumerate(blocks) if self.CALLER in b)]

        depth = 0
        hidden = []
        for number, line in enumerate(home.split("\n"), start=1):
            stripped = line.strip()
            if re.match(r"^\(function\b", stripped):
                depth += 1
            elif re.match(r"^\}\)\(\);$", stripped):
                depth -= 1
            for declaration in self.CALLED_FROM_BOOT:
                if declaration in stripped and depth > 0:
                    hidden.append(f"{declaration} on line {number}")
        self.assertEqual([], hidden, "declared inside a closure: " + ", ".join(hidden))

    def test_the_calendar_keeps_its_own_boots_and_stays_last(self):
        blocks = script_blocks()
        calendar = [i for i, b in enumerate(blocks) if "(function vcalBoot" in b]
        caller = next(i for i, b in enumerate(blocks) if self.CALLER in b)
        self.assertEqual([caller + 1], calendar, "the calendar block must follow the dashboard block")


class BootContractTests(unittest.TestCase):
    def test_the_new_bindings_run_on_every_visit(self):
        """htmx navigation does not re-fire DOMContentLoaded, so boot must bind."""
        boot = TEMPLATE.split("function __vaultBoot() {", 1)[1].split("\n}", 1)[0]
        for binding in ("__vaultBindReorder()", "__vaultBindReadingOrder()", "syncEdgeStrips()"):
            self.assertIn(binding, boot)

    def test_every_script_block_parses_as_javascript(self):
        """A syntax error here is invisible until the page is opened by hand.

        Skipped where node is not installed: the authoritative test run happens in
        the application image, which ships no node, and a check that fails there
        for the sake of a tool it does not have trains people to ignore it. The
        contracts above are plain text assertions and run everywhere; this one is
        the parser's opinion on top.
        """
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed in this environment")

        import subprocess
        import tempfile

        for index, block in enumerate(script_blocks()):
            cleaned = re.sub(r"\{%-?.*?-?%\}", "", block, flags=re.S)
            cleaned = re.sub(r"\{\{.*?\}\}", "null", cleaned, flags=re.S)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
                handle.write(cleaned)
                path = handle.name
            result = subprocess.run([node, "--check", path], capture_output=True, text=True)
            Path(path).unlink(missing_ok=True)
            self.assertEqual(0, result.returncode, f"block {index}: {result.stderr[:400]}")


if __name__ == "__main__":
    unittest.main()
