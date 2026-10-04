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


class BootContractTests(unittest.TestCase):
    def test_the_new_bindings_run_on_every_visit(self):
        """htmx navigation does not re-fire DOMContentLoaded, so boot must bind."""
        boot = TEMPLATE.split("function __vaultBoot() {", 1)[1].split("\n}", 1)[0]
        for binding in ("__vaultBindReorder()", "__vaultBindReadingOrder()", "syncEdgeStrips()"):
            self.assertIn(binding, boot)

    def test_every_script_block_parses_as_javascript(self):
        """A syntax error here is invisible until the page is opened by hand."""
        import subprocess
        import tempfile

        for index, block in enumerate(script_blocks()):
            cleaned = re.sub(r"\{%-?.*?-?%\}", "", block, flags=re.S)
            cleaned = re.sub(r"\{\{.*?\}\}", "null", cleaned, flags=re.S)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
                handle.write(cleaned)
                path = handle.name
            result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
            Path(path).unlink(missing_ok=True)
            self.assertEqual(0, result.returncode, f"block {index}: {result.stderr[:400]}")


if __name__ == "__main__":
    unittest.main()
