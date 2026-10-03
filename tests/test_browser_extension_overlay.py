"""The capture overlay must not stack, and stacked media must be reachable.

Two separate faults, both reported as "things overlap":

Every frame receives `arm` and each overlay covers its whole viewport, so without
a rule about who draws, a player inside a frame is boxed by the outer page *and*
by its own document. An `<iframe>` used to be counted as a media target as well,
which drew a box around the player on top of the box around the video inside it.

Stacked media on one page was unreachable independently of frames: the lookup
returned a single box, always the topmost, so a poster under a `<video>` or a
lightbox over the page it opened could not be selected at all.

These tests hold the source-level invariants. They cannot run a browser, so the
behaviour itself is verified by hand on a page with a nested player.
"""

import re
import unittest
from pathlib import Path

CONTENT = Path(__file__).resolve().parents[1] / "clients" / "browser-extension" / "content.js"
SOURCE = CONTENT.read_text(encoding="utf-8")


class OverlayOwnershipTests(unittest.TestCase):
    """Only the top frame draws; children report upward.

    Handing the overlay to whichever frame held the pointer was tried and dropped:
    the ancestor's overlay starves the descendant of pointer events, and a frame
    that gave its overlay away could not reliably take it back. These tests hold the
    single-owner rule instead.
    """

    def test_only_the_top_frame_asks_and_only_children_report(self):
        self.assertRegex(SOURCE, r"function reportTargets\(\)")
        self.assertRegex(SOURCE, r"if \(window\.top === window\.self\) return;")

    def test_the_parent_asks_its_children_once_it_is_built(self):
        self.assertIn("askChildrenForTargets()", SOURCE)

    def test_the_relayed_overlay_is_removed(self):
        for gone in ("function release(", "function watchForReturn(", "childFrameAt(event"):
            self.assertNotIn(gone, SOURCE)

    def test_a_child_outline_is_marked_so_the_click_can_be_forwarded(self):
        self.assertIn("remote: true", SOURCE)
        self.assertRegex(SOURCE, r"function forwardClick\(entry\)")
        self.assertIn("netsanctum:pick", SOURCE)

    def test_a_relayed_outline_keeps_the_description_the_child_computed(self):
        # describeBox reads the local DOM, which does not exist for a remote box.
        self.assertRegex(SOURCE, r"if \(entry\.remote\) return entry\.info")

    def test_escape_from_a_child_frame_reaches_the_overlay(self):
        # Focus inside a player meant Escape went nowhere at all before this.
        self.assertIn("netsanctum:dismiss", SOURCE)
        self.assertRegex(SOURCE, r'event\.key !== "Escape"')

    def test_escape_stops_propagation(self):
        body = SOURCE[SOURCE.index("const onKey = (event) => {") :]
        body = body[: body.index("const paint")]
        self.assertIn("event.stopPropagation()", body)

    def test_an_iframe_is_not_itself_a_capture_target(self):
        self.assertRegex(SOURCE, r'if \(tag === "iframe"\) return false;')

    def test_embed_is_still_a_target(self):
        # `embed` has no document of its own, so nothing else would find it.
        self.assertRegex(SOURCE, r'if \(tag === "embed"\) return true;')


class LayerCyclingTests(unittest.TestCase):
    def test_the_lookup_returns_the_whole_stack_not_one_box(self):
        self.assertIn("stackAt", SOURCE)
        self.assertRegex(SOURCE, r"const hits = \[\];")

    def test_layers_can_be_stepped_through(self):
        self.assertRegex(SOURCE, r"function cycleLayer|const cycleLayer")
        self.assertIn("ArrowDown", SOURCE)
        self.assertIn("ArrowUp", SOURCE)

    def test_the_choice_wraps_rather_than_stopping_at_the_last_layer(self):
        self.assertRegex(SOURCE, r"cycleLayer = \(step\) => \{[\s\S]*?% stack\.length")

    def test_a_click_takes_the_selected_layer_and_not_always_the_topmost(self):
        on_down = SOURCE[SOURCE.index("const onDown = (event) => {") :]
        on_down = on_down[: on_down.index("const onUp")]
        self.assertIn("layerIndex", on_down)

    def test_the_tooltip_says_which_layer_is_selected(self):
        self.assertRegex(SOURCE, r"слой \$\{layerIndex \+ 1\} из \$\{layerCount\}")

    def test_the_tooltip_only_shows_a_counter_when_there_is_a_stack(self):
        self.assertRegex(SOURCE, r"if \(layerCount > 1\)")

    def test_the_keys_are_discovered_without_hovering_anything(self):
        self.assertRegex(SOURCE, r"слои")

    def test_layer_keys_do_not_fire_during_a_region_drag(self):
        on_key = SOURCE[SOURCE.index("const onKey = (event) => {") :]
        on_key = on_key[: on_key.index("const paint")]
        self.assertIn("drag", on_key)

    def test_the_arrow_keys_are_swallowed_even_with_a_single_layer(self):
        # Otherwise an arrow quietly scrolls the page and the extension looks mute.
        on_key = SOURCE[SOURCE.index("const onKey = (event) => {") :]
        on_key = on_key[: on_key.index("const paint")]
        self.assertLess(on_key.index("preventDefault()"), on_key.index("stack.length < 2"))

    def test_tab_shift_tab_are_not_doubled_as_up_and_down(self):
        on_key = SOURCE[SOURCE.index("const onKey = (event) => {") :]
        on_key = on_key[: on_key.index("const paint")]
        self.assertIn('event.key === "Tab" && event.shiftKey', on_key)

    def test_leaving_the_frame_drops_the_stack(self):
        self.assertRegex(SOURCE, r"const onLeave = \(\) => \{[\s\S]*?stack = \[\];")

    def test_the_dead_single_box_lookup_is_gone(self):
        self.assertNotIn("targetAt", SOURCE)


class SourceIsValidTests(unittest.TestCase):
    def test_no_stale_reference_to_a_removed_helper(self):
        self.assertIsNone(re.search(r"\btargetAt\b", SOURCE))


if __name__ == "__main__":
    unittest.main()


class TeardownScopeTests(unittest.TestCase):
    """`cleanup` must not name anything declared inside `build`.

    It reached for `onLeave` directly and threw `ReferenceError: onLeave is not
    defined` the moment a capture was dismissed. `node --check` passes on that code:
    it is a scope error, not a syntax error, so nothing about the file's shape would
    have caught it. These tests check the shape instead.
    """

    def _cleanup_body(self) -> str:
        body = SOURCE[SOURCE.index("function cleanup() {") :]
        return body[: body.index("\n  function ")]

    def test_every_listener_teardown_goes_through_the_handlers_object(self):
        """Each detached listener must be `handlers.x`, or a module-level name.

        Asserting the name merely appears would pass on the very bug this was
        written for, since the identifier is present either way. The argument has to
        match one of the two shapes exactly.
        """
        body = self._cleanup_body()
        arguments = re.findall(r"removeEventListener\(\s*\"[a-z]+\",\s*([^)]*?)\s*[,)]", body)
        self.assertTrue(arguments, "no listener teardown found to check")
        allowed = re.compile(r"^(?:handlers\.[A-Za-z_$][\w$]*|dismiss|cleanup)$")
        for argument in arguments:
            self.assertRegex(
                argument.strip(),
                allowed,
                f"{argument.strip()!r} is not a module-level name and not off the handlers object",
            )

    @staticmethod
    def _handed_over() -> set[str]:
        # The real hand-over, not the placeholder the stale-copy path writes.
        for literal in re.findall(r"__netsanctum = \{([^}]*)\}", SOURCE):
            if "onMove" in literal:
                return {part.strip().split(":")[0].strip() for part in literal.split(",") if part.strip()}
        return set()

    def test_every_handler_the_teardown_uses_is_handed_over(self):
        used = set(re.findall(r"handlers\.([A-Za-z_$][\w$]*)", self._cleanup_body()))
        for name in used:
            self.assertIn(name, self._handed_over(), f"handlers.{name} is never provided")

    def test_the_builder_hands_over_every_handler_it_attaches(self):
        attached = set(re.findall(r"addEventListener\(\s*\"[a-z]+\",\s*([A-Za-z_$][\w$]*)", SOURCE))
        for name in attached:
            if name in {"dismiss", "cleanup"}:
                continue
            self.assertIn(name, self._handed_over(), f"{name} is attached but never handed to cleanup")
