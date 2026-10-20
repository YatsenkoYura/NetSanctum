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
        self.assertRegex(SOURCE, r"const onLeave = \(event\) => \{[\s\S]*?stack = \[\];")

    def test_leaving_does_not_drop_the_stack_for_a_finger(self):
        """A finger produces `pointerleave` the moment it lifts. Treating that as
        the pointer leaving wiped the stack after every tap, which is what made
        the layers unreachable on a touchscreen rather than merely hard."""
        on_leave = SOURCE[SOURCE.index("const onLeave = (event) => {") :]
        on_leave = on_leave[: on_leave.index("addEventListener")]
        self.assertIn('event.pointerType !== "mouse"', on_leave)
        self.assertRegex(on_leave, r'!== "mouse"\)\s*return;')

    def test_the_dead_single_box_lookup_is_gone(self):
        self.assertNotIn("targetAt", SOURCE)


class SourceIsValidTests(unittest.TestCase):
    def test_no_stale_reference_to_a_removed_helper(self):
        self.assertIsNone(re.search(r"\btargetAt\b", SOURCE))


class TouchGestureTests(unittest.TestCase):
    """A finger has no hover and no keyboard, so the taps stand in for both.

    Before this, every layer feature was reachable only through `ArrowDown`, and
    a tap that did not drag was read as "the page, as seen" — so on a phone the
    only captures on offer were a region or the whole viewport, and the stacked
    media underneath a player could not be reached at all.
    """

    def _section(self, start: str, end: str) -> str:
        return SOURCE[SOURCE.index(start) : SOURCE.index(end, SOURCE.index(start))]

    def test_a_tap_saves_and_a_double_tap_only_steps_a_layer(self):
        """Not the other way round: a tap that changes a layer is the one that
        costs a second tap to save, and a tap that saves has to be told apart
        before it is spent."""
        commit = self._section("const commitTaps = () => {", "const onTap =")
        self.assertIn("taps === 2", commit)
        self.assertIn("taps >= 3", commit)
        self.assertLess(commit.index("taps === 2"), commit.index("saveChosen"))
        self.assertIn("saveChosen(hits[Math.min(layerIndex, hits.length - 1)])", commit)

    def test_the_save_waits_for_a_second_tap_instead_of_firing_on_the_lift(self):
        self.assertIn("setTimeout(commitTaps, TAP_WINDOW_MS)", SOURCE)
        self.assertIn("const TAP_WINDOW_MS", SOURCE)

    def test_a_tap_hit_tests_the_point_the_finger_landed_on(self):
        """The stack was built in a move handler that touch never calls, so it is
        built where the tap lands instead."""
        on_tap = self._section("const onTap = (x, y) => {", "const onMove =")
        self.assertIn("stackAt(x, y)", on_tap)

    def test_stepping_off_the_end_wraps_round(self):
        commit = self._section("const commitTaps = () => {", "const onTap =")
        self.assertIn("layerIndex = (layerIndex + 1) % hits.length", commit)
        self.assertIn("layerIndex = (layerIndex - 1 + hits.length) % hits.length", commit)

    def test_a_tap_never_becomes_a_screenshot_of_the_page(self):
        """An empty tap used to mean the whole viewport. On a thumb that is one
        wobble away from an unwanted save of everything on screen."""
        on_up = self._section("const onUp = (event) => {", "const confirmViewport")
        self.assertIn("onTap(event.clientX, event.clientY)", on_up)
        self.assertIn("confirmViewport()", on_up)

    def test_a_touch_press_does_not_spend_the_capture(self):
        on_down = self._section("const onDown = (event) => {", "const onUp =")
        touch_branch = on_down[: on_down.index('event.pointerType !== "mouse"')]
        # The touch branch returns before anything can submit.
        self.assertIn("return;", touch_branch)
        self.assertNotIn("submit(", touch_branch)

    def test_the_overlay_owns_its_touches(self):
        """Without `touch-action: none` the browser scrolls the page under the
        veil and cancels the pointer stream, so a drag with a finger never
        reaches the end and the region is never captured."""
        css = self._section("const OVERLAY_CSS = `", "function buildOverlay")
        root = css[css.index(".root {") :]
        root = root[: root.index("}")]
        self.assertIn("touch-action: none", root)
        self.assertIn("user-select: none", root)

    def test_the_hint_wraps_instead_of_running_off_the_screen(self):
        """`white-space: nowrap` on a bottom bar of three phrases is the whole of
        "it doesn't fit" on a 360px screen."""
        css = self._section("const OVERLAY_CSS = `", "function buildOverlay")
        hint = css[css.index(".hint {") :]
        hint = hint[: hint.index("}")]
        self.assertIn("flex-wrap: wrap", hint)
        self.assertIn("max-width: calc(100vw - 16px)", hint)
        self.assertIn("env(safe-area-inset-bottom)", hint)
        self.assertNotIn("white-space: nowrap", hint)

    def test_the_tag_is_bounded_by_the_viewport(self):
        css = self._section("const OVERLAY_CSS = `", "function buildOverlay")
        tag = css[css.index(".tag {") :]
        tag = tag[: tag.index("}")]
        self.assertIn("calc(100vw - 8px)", tag)

    def test_the_hint_says_the_touch_gestures_rather_than_the_keys(self):
        build_hint = self._section("function buildHintInto(", "function buildOverlay()")
        self.assertIn("isCoarsePointer()", build_hint)
        self.assertIn("Double tap", build_hint)
        self.assertIn("Triple tap", build_hint)
        # The keyboard hint must not follow the finger around.
        self.assertIn("if (!isCoarsePointer())", build_hint)

    def test_coarse_pointer_is_asked_of_the_platform(self):
        """Not the user agent: a UA string cannot tell a touchscreen laptop from
        a desktop with a mouse, and getting it wrong hands both the mouse and the
        finger the wrong gesture."""
        self.assertIn('window.matchMedia("(pointer: coarse)")', SOURCE)
        self.assertNotIn("userAgent", SOURCE)


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
