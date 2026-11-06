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
from typing import ClassVar

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
        # Reordering needs an editable surface. The aggregate view is not refused
        # here: a card can still be dragged out of it into a space, and the drop
        # target decides what the gesture meant.
        for refusal in ("readOnlyMode", "packageMode"):
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

    def test_sealed_spaces_are_offered_as_nesting_targets(self):
        menu = TEMPLATE.split("function openNestMenu(event, id, element) {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("if (record.is_encrypted) return;", menu)


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

    def test_a_drop_on_a_space_or_folder_tile_moves_the_card(self):
        """The drop, run through the real handlers rather than described.

        Scoped honestly: this binds the handlers itself, so it proves what a drop
        does once bound — `test_the_folder_tiles_have_their_own_drop_binding`
        covers that boot binds them. Both paths are here because one was silently
        dead: dropping on a sidebar row worked, dropping on a folder tile inside
        the space did nothing, as those tiles are siblings of #tiles-grid and the
        reorder and stack listeners never saw them.
        """
        drag = self._build()["drag"]
        self.assertEqual({"via": "space", "cardId": 10, "collectionId": 2}, drag["sidebar"])
        self.assertTrue(drag["sidebarPrevented"], "without preventDefault the drop never fires")
        self.assertEqual(4, drag["folderTileId"])
        self.assertEqual({"via": "space", "cardId": 10, "collectionId": 4}, drag["folder"])
        self.assertTrue(drag["folderPrevented"])

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
        self.assertIn("vaultConfirm(", merge, "merging destroys a workspace and must ask")
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
        self.assertIn("vaultConfirm(", body)
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
        # The parent is read when the click happens, not when the page rendered:
        # a page rendered in one space and clicked in another would otherwise
        # create the folder in the space it was served from.
        self.assertIn('data-net-action="openWorkspaceModal"', TEMPLATE)
        self.assertIn('"$currentCollection"', TEMPLATE)

    def test_the_form_sends_the_parent_it_was_opened_with(self):
        body = TEMPLATE.split("async function submitWorkspace() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("payload.parent_id = parentId", body)
        self.assertIn("vaultNewSpaceParentId", body)

    def test_the_folder_option_is_hidden_at_the_top_level(self):
        body = TEMPLATE.split("function syncFolderOption() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("currentCollectionId === null", body)
        boot = TEMPLATE.split("function __vaultBoot() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("syncFolderOption()", boot)

    def test_a_sealed_space_can_be_created_as_a_folder(self):
        """It used to be refused in the browser and on the server, over a leak that
        had nothing to do with nesting: the sidebar listed the children of a locked
        space. The sidebar folds that branch instead, which is the actual guard."""
        body = TEMPLATE.split("async function submitWorkspace() {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("sealed && parentId !== null", body)

    def test_creating_a_space_no_longer_reloads_the_page(self):
        """The sidebar is built in JS now; a reload also threw away where you were."""
        body = TEMPLATE.split("async function submitWorkspace() {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("window.location.reload()", body)
        self.assertIn("loadVaultSummary()", body)

    def test_the_server_no_longer_refuses_sealed_nesting(self):
        service = Path("app/modules/vault/services.py").read_text()
        self.assertNotIn("Зашифрованное пространство всегда остаётся на верхнем уровне", service)
        self.assertNotIn("В зашифрованное пространство нельзя вложить другое", service)
        schema = Path("app/modules/vault/schemas.py").read_text()
        self.assertIn("parent_id: int | None = Field(default=None, ge=1)", schema)

    def test_a_locked_sealed_space_keeps_its_branch_folded(self):
        """The invariant that replaces the two refusals: a locked sealed space must
        not enumerate its children, because their names are the owner's."""
        body = TEMPLATE.split("function vaultSidebarRows(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("collection.is_encrypted && collection.is_locked", body)
        self.assertIn("vaultSidebarRow(collection, depth, false)", body)


class DeleteSpaceTests(unittest.TestCase):
    """Destroying a space is a separate thing from dissolving or merging one.

    A folder dissolves and its contents move up. A merge empties one space into
    another. Neither removes anything, and neither can be done to a sealed space
    at all — its cards cannot leave without a key. So delete is its own action,
    and it takes the passphrase because there is no copy anywhere.
    """

    def test_the_server_asks_for_the_passphrase(self):
        route = Path("app/modules/vault/router.py").read_text()
        body = route.split('@router.delete("/api/vault/collections/{coll_id}")', 1)[1].split("\n@router.", 1)[
            0
        ]

        self.assertIn("Нужен пароль для зашифрованного воркспейса", body)
        self.assertIn("Неверный пароль", body)
        self.assertIn("verify_space_passphrase", body)

    def test_the_passphrase_is_checked_by_opening_it(self):
        """Not accepted on its word: the proof has to be the one the unlock makes."""
        sealing = Path("app/modules/vault/sealing.py").read_text()
        body = sealing.split("def verify_space_passphrase(", 1)[1].split("\ndef ", 1)[0]

        self.assertIn("kek_for_wrapper", body)
        self.assertIn("verify_inbox_pub_mac", body)

    def test_a_wrong_passphrase_goes_through_the_failure_counter(self):
        route = Path("app/modules/vault/router.py").read_text()
        body = route.split('@router.delete("/api/vault/collections/{coll_id}")', 1)[1].split("\n@router.", 1)[
            0
        ]

        self.assertIn("record_unlock_failure", body)
        self.assertIn("status_code=401", body)

    def test_the_cards_go_with_the_space_and_their_files(self):
        """`collection_id` is SET NULL on delete, so a plain delete would orphan
        every card and leave its pictures on disk with no way to reach them."""
        services = Path("app/modules/vault/services.py").read_text()
        body = services.split("async def delete_collection(", 1)[1].split("\nclass ", 1)[0]

        self.assertIn("VaultItem", body)
        self.assertIn("image_path", body)
        self.assertIn("media_path", body)
        self.assertIn("media_thumbnail_path", body)
        self.assertIn("get_storage().delete_file", body)

    def test_nested_spaces_are_deleted_too(self):
        services = Path("app/modules/vault/services.py").read_text()
        body = services.split("async def delete_collection(", 1)[1].split("\nclass ", 1)[0]

        self.assertIn("VaultCollection.parent_id == coll_id", body)
        self.assertIn("await delete_collection(session, child.id)", body)

    def test_it_says_what_it_removed(self):
        services = Path("app/modules/vault/services.py").read_text()
        body = services.split("async def delete_collection(", 1)[1].split("\nclass ", 1)[0]

        self.assertIn('"cards": 0', body)
        self.assertIn('"spaces": 0', body)

    def test_the_ui_asks_before_it_calls(self):
        body = TEMPLATE.split("async function deleteSpace(", 1)[1].split("\nfunction ", 1)[0]

        self.assertIn("vaultConfirm(", body)
        self.assertIn("Отменить будет нельзя, копии нет", body)

    def test_a_sealed_space_gets_the_password_prompt(self):
        body = TEMPLATE.split("async function deleteSpace(", 1)[1].split("\nfunction ", 1)[0]

        self.assertIn("vaultPromptPassword", body)
        self.assertIn("if (collection.is_encrypted)", body)

    def test_the_password_prompt_is_not_the_unlock_field(self):
        """Unlocking is ordinary and its field is usually on screen; a passphrase
        typed for one purpose should not be sitting in the DOM for another."""
        self.assertIn('id="modal-passphrase"', TEMPLATE)
        self.assertIn('id="space-passphrase-input"', TEMPLATE)
        self.assertIn('id="unlock-passphrase-input"', TEMPLATE)
        # Distinct elements, each with one id: sharing the unlock field would put
        # a passphrase typed for one purpose into the DOM for another.
        prompt = TEMPLATE.split('id="space-passphrase-input"', 1)[0]
        unlock = TEMPLATE.split('id="unlock-passphrase-input"', 1)[0]
        self.assertNotEqual(prompt, unlock)
        self.assertEqual(1, TEMPLATE.count('id="space-passphrase-input"'))

    def test_deleting_the_open_space_leaves_it_open(self):
        body = TEMPLATE.split("async function deleteSpace(", 1)[1].split("\nfunction ", 1)[0]

        self.assertIn("vaultIsDescendantOf", body)
        self.assertIn("currentCollectionId = null", body)
        self.assertIn("vaultUnlockToken = ''", body)


class LockedSerializationTests(unittest.TestCase):
    """A locked view must not invent nulls the schema forbids.

    `progress_current` and `rewatch_count` are NOT NULL columns and the response
    schema declares them as integers, so blanking them to None in the locked
    branch turned every read, create and edit of a card in a locked space into a
    500 — which is how the sealed-counters change of the previous cycle turned out
    to be reachable.
    """

    def test_the_locked_branch_uses_the_zeroing_table(self):
        route = Path("app/modules/vault/router.py").read_text()
        branch = route.split("if sealed and locked:", 1)[1].split("return serialized", 1)[0]

        self.assertIn("SEALED_ZEROED_FIELDS.get(field)", branch)
        self.assertNotIn("serialized[field] = None", branch)

    def test_every_sealed_field_settles_on_something_valid(self):
        from app.modules.vault.schemas import VaultItemResponse
        from app.modules.vault.sealing import SEALED_FIELDS, SEALED_ZEROED_FIELDS

        route = Path("app/modules/vault/router.py").read_text()
        branch = route.split("if sealed and locked:", 1)[1].split("return serialized", 1)[0]
        required = {name for name, field in VaultItemResponse.model_fields.items() if field.is_required()}
        # `title` and `tags` are settled by explicit lines — a locked card shows
        # its alias and no tags. What is left is the set that produces a 500.
        handled = set(re.findall(r'serialized\["(\w+)"\]\s*=', branch))

        for field in sorted((set(SEALED_FIELDS) & required) - handled):
            self.assertIn(field, SEALED_ZEROED_FIELDS, f"{field} is sealed, required and never given a value")
            self.assertIsNotNone(SEALED_ZEROED_FIELDS[field])
        self.assertEqual({"progress_current", "rewatch_count"}, set(SEALED_ZEROED_FIELDS))


class MenuAnchorTests(unittest.TestCase):
    """A floating menu needs the button's rectangle, and the event will not give it.

    A delegated action runs inside a listener on the document, so
    `event.currentTarget` is that document — which has no
    `getBoundingClientRect`, and the menu threw a TypeError on every open. The
    element travels with the call instead.
    """

    def test_nothing_reads_a_rectangle_off_the_event(self):
        self.assertNotIn("event.currentTarget.getBoundingClientRect", TEMPLATE)

    def test_there_is_one_helper_and_the_menus_use_it(self):
        self.assertIn("function anchorRect(event, element)", TEMPLATE)
        self.assertEqual(3, TEMPLATE.count("const anchor = anchorRect(event, element);"))

    def test_the_helper_falls_back_instead_of_throwing(self):
        """A caller that cannot say which element it came from gets a rectangle
        at the origin rather than a TypeError in the middle of a click."""
        self.assertIn("typeof source.getBoundingClientRect === 'function'", TEMPLATE)
        self.assertIn("return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };", TEMPLATE)

    def test_each_menu_takes_the_element(self):
        for name in (
            "openMoveCardMenu(event, cardId, element)",
            "openMoveMenu(event, id, element)",
            "openNestMenu(event, id, element)",
        ):
            self.assertIn(name, TEMPLATE)

    def test_the_delegated_wrappers_hand_over_the_element_they_were_called_on(self):
        self.assertIn("openMoveCardMenu(window.netSanctumEvent, id, this)", TEMPLATE)
        self.assertIn("openMoveMenu(window.netSanctumEvent, id, this)", TEMPLATE)

    def test_a_direct_listener_passes_its_own_target(self):
        self.assertIn("openNestMenu(event, collection.id, event.currentTarget)", TEMPLATE)


class ConfirmationDialogTests(unittest.TestCase):
    """Destructive actions get a dialog of our own, not `confirm()`.

    The native one blocks the page, cannot be styled, and returns focus to
    whatever was pressed — so the button that opened it can be re-activated by
    the same keypress that dismissed the dialog. On a card with a picture that
    read as an endless loop of dialogs with a spinning page behind it, and there
    was no way to tell a native dialog's «Отмена» from a broken button.
    """

    def test_no_native_confirm_survives(self):
        code = "\n".join(
            line for line in TEMPLATE.splitlines() if not line.strip().startswith(("//", "*", "<!--"))
        )
        self.assertNotRegex(code, r"(?<![\w.])confirm\s*\(")

    def test_the_dialog_is_in_the_markup(self):
        for marker in (
            'id="modal-confirm"',
            'id="confirm-message"',
            'id="confirm-ok"',
            'id="confirm-cancel"',
        ):
            self.assertIn(marker, TEMPLATE)

    def test_both_buttons_say_what_they_do(self):
        self.assertIn(">Отмена</button>", TEMPLATE)
        self.assertIn(">Удалить</button>", TEMPLATE)

    def test_the_dialog_resolves_a_promise(self):
        """One dialog, one answer: a second call while it is open replaces the
        resolver rather than leaving two of them."""
        self.assertIn("function vaultConfirm(message", TEMPLATE)
        self.assertIn("return new Promise(resolve => { vaultConfirmResolver = resolve; });", TEMPLATE)
        self.assertIn("vaultConfirmResolver = null;", TEMPLATE)

    def test_a_destructive_one_is_labelled_dangerously(self):
        self.assertIn("confirmLabel = 'Удалить'", TEMPLATE)

    def test_escape_and_the_backdrop_answer_no(self):
        self.assertIn("event.target.id === 'modal-confirm'", TEMPLATE)

    def test_the_listeners_are_bound_once(self):
        """The buttons are in the server-rendered layout, so binding them on
        every htmx visit would attach a second listener per visit."""
        self.assertIn("window.__vaultConfirmBound", TEMPLATE)

    def test_the_delegate_path_is_not_used_for_it(self):
        """Its buttons carry no `data-net-action`, so the shared dispatcher cannot
        answer them a second time."""
        for line in TEMPLATE.splitlines():
            if 'id="confirm-ok"' in line or 'id="confirm-cancel"' in line:
                self.assertNotIn("data-net-action", line)


class DoubleSubmitTests(unittest.TestCase):
    """One press, one request.

    Two spaces 197 milliseconds apart with the same name is what a modal does
    when its button and its Enter key both reach the same function and nothing
    refuses the second one. The second space is sealed and holds a key, so this
    is not a cosmetic duplicate.
    """

    def test_the_guard_exists(self):
        self.assertIn("var submitInFlight = {};", TEMPLATE)
        self.assertIn("if (submitInFlight[key]) return null;", TEMPLATE)
        self.assertIn("async function submitOnce(", TEMPLATE)

    def test_the_flag_is_released_even_when_the_request_fails(self):
        """A guard that never clears is a form that works once."""
        body = TEMPLATE.split("async function submitOnce(", 1)[1].split("\nasync function", 1)[0]

        self.assertIn("finally", body)

    def test_every_modal_create_goes_through_it(self):
        for key in ("'workspace'", "'link'"):
            self.assertIn(f"submitOnce({key}", TEMPLATE)

    def test_unlock_uses_the_guard_too(self):
        """It handles its own errors, so it cannot use `submitOnce`."""
        body = TEMPLATE.split("async function submitUnlock()", 1)[1].split("\nasync function", 1)[0]

        self.assertIn("if (submitInFlight.unlock) return;", body)
        self.assertIn("submitInFlight.unlock = false;", body)

    def test_the_buttons_are_disabled_while_a_submit_is_in_flight(self):
        """A refusal with no feedback reads as a broken button."""
        self.assertIn("function setSubmitButtonsDisabled(", TEMPLATE)
        self.assertIn("button.disabled = disabled;", TEMPLATE)


class CardOntoSpaceGestureTests(unittest.TestCase):
    """Dropping a card on a space moves it there.

    The gesture did not exist: the sidebar only accepted drags that started
    inside it, and a card drag starts in the grid, so dropping a card on a space
    did nothing at all.
    """

    def test_the_sidebar_accepts_a_card_drag_and_moves_the_card(self):
        body = TEMPLATE.split("async function vaultMoveCardToSpace(", 1)[1].split("\n}", 1)[0]
        self.assertIn("collection_id: collectionId", body)
        self.assertIn("pathSegment(cardId)", body)
        drop = TEMPLATE.split("list.addEventListener('drop', function (e) {", 1)[1].split("\n  });", 1)[0]
        self.assertIn("vaultMoveCardToSpace(cardId, targetId)", drop)

    def test_dropping_on_all_cards_unfiles_the_card(self):
        self.assertIn(
            "collectionId === null",
            TEMPLATE.split("function vaultCardDropTargetValid(", 1)[1].split("\n}", 1)[0],
        )

    def test_a_sealed_space_is_not_refused_as_a_target(self):
        """It used to be refused for a plain card, which made a sealed folder
        unusable: the move is allowed now and the card is sealed on the way in."""
        body = TEMPLATE.split("function vaultCardDropTargetValid(", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("target.is_encrypted", body)
        self.assertIn("!target", body)

    def test_the_server_refuses_the_moves_the_sidebar_hides(self):
        service = Path("app/modules/vault/services.py").read_text()
        self.assertIn("_assert_can_move_card", service)
        self.assertIn("Зашифрованную карточку нельзя перенести в другое пространство", service)

    def test_the_aggregate_view_still_allows_a_card_to_be_dragged_out(self):
        """It cannot be reordered there, but it can be filed into a space."""
        refusal = TEMPLATE.split("function vaultReorderRefusal() {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("!currentCollectionId", refusal)
        commit = TEMPLATE.split("async function vaultCommitReorder() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("Порядок задаётся внутри пространства", commit)

    def test_the_three_drop_targets_are_three_different_meanings(self):
        """A card on a card reorders; a card on a grip stacks; a card on a space moves."""
        self.assertIn("if (e.target.closest && e.target.closest('[data-grip]')) return;", TEMPLATE)
        self.assertIn("/api/vault/items/move", TEMPLATE)
        self.assertIn("vaultStackDrop(fromId, toId)", TEMPLATE)

    def test_the_folder_tiles_have_their_own_drop_binding(self):
        body = TEMPLATE.split("function __vaultBindFolderDrop() {", 1)[1].split("\n}", 1)[0]
        self.assertIn(".space-tile", body)
        self.assertIn("vaultMoveCardToSpace(cardId, found.id)", body)
        boot = TEMPLATE.split("function __vaultBoot() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("__vaultBindFolderDrop()", boot)

    def test_a_card_can_also_be_filed_without_dragging(self):
        """HTML5 drag does not exist on a phone, so the move needs a button too."""
        footer = TEMPLATE.split("function tileFooterHtml(", 1)[1].split("\nfunction openMoveCardMenu", 1)[0]
        # A wrapper, not a call in an attribute: the move needs the event in
        # flight to stop the tile from also opening under the click.
        self.assertIn('data-net-action="openMoveCardMenuFromTile"', footer)
        self.assertIn("function openMoveCardMenu(", TEMPLATE)
        # The exact name matters: a menu button once called a function that did
        # not exist, four letters short of the real one.
        self.assertIn('data-net-action="vaultMoveCardToSpace"', TEMPLATE)

    def test_a_plain_card_may_go_into_a_sealed_space_where_it_gets_sealed(self):
        """The server seals it on the way in, needing only the target's public key."""
        for body in (
            TEMPLATE.split("function openMoveCardMenu(", 1)[1].split("\n}", 1)[0],
            TEMPLATE.split("function vaultCardDropTargetValid(", 1)[1].split("\n}", 1)[0],
        ):
            self.assertNotIn("is_encrypted && !card.is_sealed", body)

    def test_a_sealed_card_is_still_kept_away_from_a_plain_space(self):
        """Its key belongs to the space it came from, so it would arrive unopenable."""
        self.assertIn(
            "if (!target.is_encrypted && card.is_sealed) return;",
            TEMPLATE.split("function openMoveCardMenu(", 1)[1].split("\n}", 1)[0],
        )

    def test_a_sealed_card_is_nowhere_offered_to_leave_its_space(self):
        """Both paths used to offer it: the menu's "no space" entry and a drop on
        "Все карточки". The server refused neither with a word — it tripped over a
        missing target and answered 500."""
        menu = TEMPLATE.split("function openMoveCardMenu(", 1)[1].split("\n}", 1)[0]
        self.assertIn("const targets = card.is_sealed ? []", menu)
        drop = TEMPLATE.split("function vaultCardDropTargetValid(", 1)[1].split("\n}", 1)[0]
        self.assertIn("if (collectionId === null) return !card.is_sealed;", drop)


class InlineHandlerTests(unittest.TestCase):
    """Every `onclick="fn(...)"` in the template must name a function that exists.

    A menu button once called `vaultMoveCardTo` while the function was called
    `vaultMoveCardToSpace`. Nothing caught it: both files parsed, and the name is
    right up to the last four letters. Clicking it threw in the browser, which is
    the only place it was ever going to be noticed.
    """

    # Provided by the page shell, not by this template.
    SHELL_PROVIDED: ClassVar[set[str]] = {"sendToOutpost"}

    def test_every_inline_handler_is_defined(self):
        defined = set(re.findall(r"^function ([A-Za-z_$][\w$]*)\(", TEMPLATE, re.M))
        defined |= set(re.findall(r"^async function ([A-Za-z_$][\w$]*)\(", TEMPLATE, re.M))
        called = set(re.findall(r'on(?:click|change|input)="([A-Za-z_$][\w$]*)\(', TEMPLATE))
        missing = sorted(called - defined - self.SHELL_PROVIDED)
        self.assertEqual([], missing, f"обработчики не определены: {missing}")


class StaleSpaceRecoveryTests(unittest.TestCase):
    """A space that no longer exists must not leave the grid empty.

    Dissolving the folder you are standing in deleted it while `currentCollectionId`
    still pointed at it, so the next load asked for a space that was gone and came
    back with nothing. F5 fixed it, because a reload resets the state from the URL.
    """

    def test_the_summary_recovers_when_the_open_space_is_gone(self):
        body = TEMPLATE.split("async function loadVaultSummary() {", 1)[1].split("\n  } catch", 1)[0]
        self.assertIn("!vaultCollectionsById[currentCollectionId]", body)
        self.assertIn("currentCollectionId = null;", body)

    def test_dissolving_falls_back_to_the_parent_when_that_is_where_we_were(self):
        body = TEMPLATE.split("async function dissolveSpace(", 1)[1].split("\n}", 1)[0]
        self.assertIn("vaultParentOf", body)
        self.assertIn("selectWorkspace(currentCollectionId", body)


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


class SealedFileServingContractTests(unittest.TestCase):
    """A locked vault's files reached the browser with no authorization at all.

    The file endpoints now check the lock, and that moves work into the page:
    `<img>` cannot send the unlock header, so sealed pictures come from an
    authorized `fetch()` into a blob URL. The wiring *is* the feature here —
    a tile that falls back to a plain `src` gets a 423 and a broken picture.
    """

    def test_a_sealed_card_own_file_goes_through_the_binder(self):
        tag = TEMPLATE.split("function vaultImgTag(", 1)[1].split("\n}", 1)[0]
        self.assertIn("data-vault-file", tag, "a sealed card's file must not ride a plain src")
        self.assertIn("isVaultFileUrl(url)", tag, "only our own file endpoints are fetched with the header")

    def test_a_remote_picture_never_gets_the_unlock_header(self):
        """A cross-origin fetch with a custom header dies in preflight."""
        classify = TEMPLATE.split("function isVaultFileUrl(", 1)[1].split("\n}", 1)[0]
        self.assertIn("parsed.origin === window.location.origin", classify)
        self.assertIn("parsed.pathname.startsWith('/api/vault/items/')", classify)

    def test_the_binder_sends_the_unlock_header(self):
        binder = TEMPLATE.split("async function bindVaultFileImages(", 1)[1].split("\n}", 1)[0]
        self.assertIn("vaultUnlockHeader", binder)
        self.assertIn("URL.createObjectURL", binder)
        self.assertIn("URL.revokeObjectURL", TEMPLATE, "blob URLs must be revocable or they leak bytes")

    def test_the_grid_binds_its_pictures_and_revokes_the_old_blobs(self):
        render = TEMPLATE.split("function renderTiles(items)", 1)[1].split("\nasync function", 1)[0]
        self.assertIn("revokeVaultBlobUrls();", render, "a rebuilt grid must not keep the old blobs alive")
        self.assertIn("bindVaultFileImages(grid);", render)

    def test_the_player_uses_the_signed_url_the_server_minted(self):
        media = TEMPLATE.split("function mediaUrl(item)", 1)[1].split("\n}", 1)[0]
        self.assertIn("item.media_url", media, "a sealed card's video must not be rebuilt client-side")
        retry = TEMPLATE.split("async function retryVideoDownload(", 1)[1].split("\n}", 1)[0]
        self.assertIn("/video/retry", retry)

    def test_a_locked_card_says_so_instead_of_showing_a_broken_frame(self):
        media_view = TEMPLATE.split("function openMediaView(item)", 1)[1].split("\n}", 1)[0]
        self.assertIn("locked", media_view)
        self.assertIn("🔒", media_view)
        self.assertIn("bindVaultFileImages(stage);", media_view)


class BlindMediaWatchContractTests(unittest.TestCase):
    """A blind file finishes on the worker, out from under the open card.

    The finalize runs after an unlock, so the card that says "шифруется" has
    to notice when that happened — otherwise the owner is left reading a stale
    state and reopening the card by hand to find out the player is back.
    """

    def test_a_waiting_video_watches_its_own_status(self):
        media_view = TEMPLATE.split("function openMediaView(item)", 1)[1].split("\n}", 1)[0]
        self.assertIn("watchMediaStatus(card.id);", media_view)
        self.assertIn("stopMediaWatch();", media_view, "a playable card must not keep polling")

    def test_the_watch_polls_with_the_unlock_header(self):
        watch = TEMPLATE.split("function watchMediaStatus(itemId)", 1)[1].split("\n}", 1)[0]
        self.assertIn("requestJson", watch, "the poll has to carry the tab's unlock, like every other read")
        self.assertIn("/api/vault/items/", watch)
        self.assertIn("updateCachedItem(fresh)", watch)

    def test_the_watch_stops_when_the_card_settles_or_the_view_closes(self):
        watch = TEMPLATE.split("function watchMediaStatus(itemId)", 1)[1].split("\n}", 1)[0]
        self.assertIn("view-media-container", watch, "leaving the card must stop the poll")
        self.assertIn("openId !== String(itemId)", watch, "switching cards must stop the poll")
        self.assertIn("settled", watch, "completed and failed cards are terminal")


if __name__ == "__main__":
    unittest.main()
