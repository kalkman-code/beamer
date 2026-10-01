import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
from gestures import (
    CHORD,
    DOCK_PHASE_ENDED,
    DOCK_SWIPE_HID_TYPE,
    GESTURE_TYPE,
    MAGNIFY_TYPE,
    MOTION_HORIZONTAL,
    MOTION_PINCH,
    MOTION_VERTICAL,
    SMART_MAGNIFY_TYPE,
    SWIPE_TYPE,
    NS_EVENT_PHASE_CANCELLED,
    NS_EVENT_PHASE_ENDED,
    DockSwipeClassifier,
    GestureTranslator,
    chord_for,
    sign_table_for,
)


class FakeNSEvent:
    """Fakes the surface GestureTranslator reads off a converted NSEvent:
    `.magnification`, `.deltaX`, `.deltaY`, `.phase`. Kept independent of
    AppKit/Quartz entirely, per the module's design."""

    def __init__(self, magnification=0.0, deltaX=0.0, deltaY=0.0, phase=0):
        self.magnification = magnification
        self.deltaX = deltaX
        self.deltaY = deltaY
        self.phase = phase


class BrokenNSEvent:
    """Raises when any attribute is read, simulating an unreadable/converted
    event (e.g. an OS version whose NSEvent bridging doesn't support this
    accessor for this event subtype)."""

    def __getattr__(self, name):
        raise RuntimeError(f"synthetic failure reading {name!r}")


def chord(action):
    return [{"type": CHORD, "data": {"action": action}}]


def zoom_in_triple():
    return chord("zoom_in")


def zoom_out_triple():
    return chord("zoom_out")


def keys(messages):
    return [(m["type"], m["data"].get("key")) for m in messages]


class ChordShapeTests(unittest.TestCase):
    """What a CHORD becomes once the machine it is sent to is known."""

    def test_a_zoom_to_a_pc_is_control_wheel(self):
        self.assertEqual(chord_for("zoom_in", "windows"), [
            {"type": protocol.MSG_KEYDOWN, "data": {"key": "ctrl"}},
            protocol.scroll_msg(dy=1, dx=0, mode="line"),
            {"type": protocol.MSG_KEYUP, "data": {"key": "ctrl"}},
        ])
        self.assertEqual(chord_for("zoom_out", "windows")[1], protocol.scroll_msg(dy=-1, dx=0, mode="line"))

    def test_a_page_swipe_to_a_pc_is_alt_arrow(self):
        self.assertEqual(keys(chord_for("back", "windows")), [
            (protocol.MSG_KEYDOWN, "alt"), (protocol.MSG_KEYDOWN, "left"), (protocol.MSG_KEYUP, "left"), (protocol.MSG_KEYUP, "alt")])
        self.assertEqual(keys(chord_for("forward", "windows"))[1], (protocol.MSG_KEYDOWN, "right"))

    def test_a_zoom_to_a_mac_is_command_plus_and_minus(self):
        self.assertEqual(keys(chord_for("zoom_in", "mac")), [
            (protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "="), (protocol.MSG_KEYUP, "="), (protocol.MSG_KEYUP, "cmd")])
        self.assertEqual(keys(chord_for("zoom_out", "mac"))[1], (protocol.MSG_KEYDOWN, "-"))

    def test_a_page_swipe_to_a_mac_is_command_bracket(self):
        self.assertEqual(keys(chord_for("back", "mac"))[:2], [(protocol.MSG_KEYDOWN, "cmd"), (protocol.MSG_KEYDOWN, "[")])
        self.assertEqual(keys(chord_for("forward", "mac"))[1], (protocol.MSG_KEYDOWN, "]"))

    def test_the_mac_chord_names_its_us_key_so_another_layout_still_presses_a_key(self):
        downs = [m["data"] for m in chord_for("back", "mac") if m["type"] == protocol.MSG_KEYDOWN]
        self.assertEqual(downs, [{"key": "cmd"}, {"key": "[", "us": "["}])

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            chord_for("sideways", "mac")


class GestureTranslatorWantsTests(unittest.TestCase):
    def test_wants_all_four_reserved_gesture_types(self):
        translator = GestureTranslator()
        for event_type in (GESTURE_TYPE, MAGNIFY_TYPE, SWIPE_TYPE, SMART_MAGNIFY_TYPE):
            self.assertTrue(translator.wants(event_type))

    def test_does_not_want_ordinary_event_types(self):
        translator = GestureTranslator()
        for event_type in (1, 5, 10, 11, 12, 22, 28, 33):
            self.assertFalse(translator.wants(event_type))


class MagnifyTranslationTests(unittest.TestCase):
    def setUp(self):
        self.translator = GestureTranslator()

    def test_single_event_crossing_the_step_emits_one_zoom_in_triple(self):
        messages = self.translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=0.05))
        self.assertEqual(messages, zoom_in_triple())

    def test_negative_magnification_emits_zoom_out(self):
        messages = self.translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=-0.05))
        self.assertEqual(messages, zoom_out_triple())

    def test_remainder_is_carried_across_events(self):
        # 0.03 alone is below the 0.05 step -- nothing fires yet.
        first = self.translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=0.03))
        self.assertEqual(first, [])
        # 0.03 + 0.03 = 0.06 -- crosses the step once, 0.01 remainder carried.
        second = self.translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=0.03))
        self.assertEqual(second, zoom_in_triple())
        self.assertAlmostEqual(self.translator._magnify_accum, 0.01)

    def test_large_single_event_emits_multiple_steps(self):
        messages = self.translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=0.12))
        self.assertEqual(messages, zoom_in_triple() * 2)
        self.assertAlmostEqual(self.translator._magnify_accum, 0.02)

    def test_phase_ended_resets_accumulation_even_below_threshold(self):
        below_threshold = self.translator.translate(
            MAGNIFY_TYPE, FakeNSEvent(magnification=0.02, phase=NS_EVENT_PHASE_ENDED)
        )
        self.assertEqual(below_threshold, [])
        self.assertEqual(self.translator._magnify_accum, 0.0)
        # A fresh 0.04 alone must not fire: the prior 0.02 was dropped by the
        # phase-ended reset rather than still being carried.
        after_reset = self.translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=0.04))
        self.assertEqual(after_reset, [])

    def test_phase_cancelled_also_resets_accumulation(self):
        self.translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=0.02))
        self.translator.translate(
            MAGNIFY_TYPE, FakeNSEvent(magnification=0.0, phase=NS_EVENT_PHASE_CANCELLED)
        )
        self.assertEqual(self.translator._magnify_accum, 0.0)

    def test_unknown_phase_attribute_defaults_defensively_to_zero(self):
        # An event exposing no usable `.phase` at all (getattr default) must
        # not raise and must not reset accumulation.
        class NoPhaseEvent:
            def __init__(self, magnification):
                self.magnification = magnification

        messages = self.translator.translate(MAGNIFY_TYPE, NoPhaseEvent(0.02))
        self.assertEqual(messages, [])
        self.assertAlmostEqual(self.translator._magnify_accum, 0.02)


class SwipeTranslationTests(unittest.TestCase):
    def setUp(self):
        self.translator = GestureTranslator()

    def test_positive_delta_x_is_back(self):
        messages = self.translator.translate(SWIPE_TYPE, FakeNSEvent(deltaX=1.0))
        self.assertEqual(messages, chord("back"))

    def test_negative_delta_x_is_forward(self):
        messages = self.translator.translate(SWIPE_TYPE, FakeNSEvent(deltaX=-1.0))
        self.assertEqual(messages, chord("forward"))

    def test_vertical_only_swipe_is_ignored(self):
        messages = self.translator.translate(SWIPE_TYPE, FakeNSEvent(deltaX=0.0, deltaY=1.0))
        self.assertEqual(messages, [])


class ReservedGestureTypeTests(unittest.TestCase):
    def test_gesture_and_smart_magnify_are_reserved_no_ops(self):
        translator = GestureTranslator()
        self.assertEqual(translator.translate(GESTURE_TYPE, FakeNSEvent()), [])
        self.assertEqual(translator.translate(SMART_MAGNIFY_TYPE, FakeNSEvent()), [])


class FakeDockFields:
    def __init__(self, motion, progress, phase=DOCK_PHASE_ENDED, velocity=0.0, hid=DOCK_SWIPE_HID_TYPE):
        self.hid = hid
        self.motion = motion
        self.phase = phase
        self.progress = progress
        self.velocity = velocity


class DockSwipeClassifierTests(unittest.TestCase):
    """Signs as measured on macOS 26.6.2."""

    def setUp(self):
        self.classifier = DockSwipeClassifier(macos_major=26)

    def classify(self, **kwargs):
        messages = self.classifier.translate(FakeDockFields(**kwargs))
        return [m["data"]["name"] for m in messages]

    def test_vertical_negative_is_up_and_positive_is_down(self):
        self.assertEqual(self.classify(motion=MOTION_VERTICAL, progress=-0.32), ["swipe_up"])
        self.assertEqual(self.classify(motion=MOTION_VERTICAL, progress=0.33), ["swipe_down"])

    def test_horizontal_positive_is_left_and_negative_is_right(self):
        self.assertEqual(self.classify(motion=MOTION_HORIZONTAL, progress=0.9), ["swipe_left"])
        self.assertEqual(self.classify(motion=MOTION_HORIZONTAL, progress=-0.81), ["swipe_right"])

    def test_pinch_axis_negative_is_spread_and_positive_is_pinch(self):
        self.assertEqual(self.classify(motion=MOTION_PINCH, progress=-0.81), ["spread"])
        self.assertEqual(self.classify(motion=MOTION_PINCH, progress=1.08), ["pinch"])

    def test_only_the_ended_phase_fires(self):
        for phase in (1, 2, 8):
            self.assertEqual(self.classify(motion=MOTION_VERTICAL, progress=-0.4, phase=phase), [])

    def test_a_twitch_below_threshold_uses_velocity_or_nothing(self):
        self.assertEqual(self.classify(motion=MOTION_VERTICAL, progress=-0.05), [])
        self.assertEqual(
            self.classify(motion=MOTION_VERTICAL, progress=-0.05, velocity=2.0), ["swipe_down"]
        )

    def test_other_hid_types_and_unknown_axes_are_ignored(self):
        self.assertEqual(self.classify(motion=MOTION_VERTICAL, progress=-0.4, hid=6), [])
        self.assertEqual(self.classify(motion=9, progress=-0.4), [])

    def test_macos_27_flips_only_the_horizontal_axis(self):
        classifier = DockSwipeClassifier(macos_major=27)
        names = [m["data"]["name"] for m in classifier.translate(FakeDockFields(MOTION_HORIZONTAL, 0.9))]
        self.assertEqual(names, ["swipe_right"])
        names = [m["data"]["name"] for m in classifier.translate(FakeDockFields(MOTION_VERTICAL, -0.4))]
        self.assertEqual(names, ["swipe_up"])

    def test_unknown_version_takes_the_newest_table(self):
        self.assertIs(sign_table_for(99), sign_table_for(27))

    def test_broken_fields_never_raise(self):
        self.assertEqual(self.classifier.translate(BrokenNSEvent()), [])
        self.assertEqual(self.classify(motion=MOTION_VERTICAL, progress=-0.4), ["swipe_up"])


class ErrorHandlingTests(unittest.TestCase):
    def test_broken_event_never_raises_and_returns_empty(self):
        translator = GestureTranslator()
        self.assertEqual(translator.translate(MAGNIFY_TYPE, BrokenNSEvent()), [])
        self.assertEqual(translator.translate(SWIPE_TYPE, BrokenNSEvent()), [])

    def test_broken_event_does_not_corrupt_later_accumulation(self):
        translator = GestureTranslator()
        translator.translate(MAGNIFY_TYPE, BrokenNSEvent())
        # A normal event afterwards must translate exactly as if the broken
        # one had never happened.
        messages = translator.translate(MAGNIFY_TYPE, FakeNSEvent(magnification=0.05))
        self.assertEqual(messages, zoom_in_triple())


if __name__ == "__main__":
    unittest.main()
