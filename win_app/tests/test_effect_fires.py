"""Traces every crossing-effect "move" from sender.py/receiver.py's pressure and arrival
callbacks to the call that actually reaches EffectOverlay or EdgeGlow, at the one hop that has no
test today: kvm_bridge_win.py's WindowsApplication._on_pressure / _on_arrival.

sender.py's LinkSender._notify_pressure(edge, pressure, crossed, part) and its arrival_callback --
an edge push, a Part-of-the-edge push, a corner push, a crossing, a switch home -- are already
proven by test_links.py's EdgeTests, CornerTests, TakingTests and OnwardTests. receiver.py's
pressure_callback and arrival_callback (the return-edge push and its arrival) are proven by
test_receiver.py's tests around line 716. Neither is repeated here: these tests start from the
(edge, pressure, crossed, part) / (method, edge, x, y) values those callbacks already produce and
check where they land.

_on_pressure and _on_arrival read self._effect_overlay() / self._switch_overlay() / self.effects /
self.glow / self._config, so a SimpleNamespace "owner" stands in for WindowsApplication -- the
same pattern test_effect_overlay.py's test_the_app_shows_a_switch_only_with_both_switches_on uses
to call WindowsApplication._switch_overlay directly, without building a whole app (tray icon,
hooks, Announcer, a real receiver socket). _effect_overlay and _switch_overlay are themselves
mocked here, at the overlay: what decides whether the chosen style
counts as "an effect" is config plumbing already covered by test_effect_overlay.py, not this
file's concern."""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    from PySide6.QtWidgets import QApplication

    import effect_overlay
    import kvm_bridge_win
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None


def _owner(edge_glow=True, glow_style="glow", glow_colour="signal", shortcut_arrival=True,
           shortcut_arrival_style="match", effect_size="medium", effects=None, glow=None):
    config = SimpleNamespace(
        edge_glow=edge_glow, glow_style=glow_style, glow_colour=glow_colour,
        shortcut_arrival=shortcut_arrival, shortcut_arrival_style=shortcut_arrival_style, effect_length="normal",
        effect_size=effect_size,
    )
    return SimpleNamespace(_closing=False, _config=config, effects=effects, glow=glow)


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class PressureRoutingTest(unittest.TestCase):
    """The Mac driving this PC and pushing back at the way home (receiver.py's pressure_callback,
    always with part=None) and this PC's own crossing out (sender.py's, part carrying Part of the
    edge) both end up on the same bridge.pressure signal and the same _on_pressure -- nothing
    here tells the two apart, which is why one set of tests covers both directions."""

    def test_an_edge_push_reaches_the_effects_overlay_when_the_style_is_an_effect(self):
        owner = _owner(glow_style="rupture")
        overlay = mock.Mock()
        owner._effect_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 0.4, False)
        overlay.push.assert_called_once_with("left", 0.4, False, part=None)

    def test_an_edge_push_reaches_edge_glow_when_the_style_is_glow(self):
        owner = _owner(glow_style="glow", glow_colour="ocean", glow=mock.Mock())
        owner._effect_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 0.4, False)
        owner.glow.configure.assert_called_once_with("glow", "ocean", "normal", "medium")
        owner.glow.set_pressure.assert_called_once_with("left", 0.4, False, None)

    def test_a_beam_style_push_configures_the_glow_as_beam(self):
        owner = _owner(glow_style="beam", glow=mock.Mock())
        owner._effect_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 0.4, False)
        owner.glow.configure.assert_called_once_with("beam", "signal", "normal", "medium")

    def test_the_selected_size_reaches_the_glow_renderer(self):
        owner = _owner(glow_style="glow", glow=mock.Mock(), effect_size="large")
        owner._effect_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 0.4, False)
        owner.glow.configure.assert_called_once_with("glow", "signal", "normal", "large")

    def test_an_effect_that_failed_earlier_falls_back_to_plain_glow(self):
        """_on_pressure's own comment: an effect that failed earlier in the run falls back to
        today's glow. A style that is neither "glow" nor "beam" (an effect id) still configures
        EdgeGlow as plain "glow" -- the effect id itself is never passed through."""
        owner = _owner(glow_style="wormhole", glow_colour="mono", glow=mock.Mock())
        owner._effect_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 0.4, False)
        owner.glow.configure.assert_called_once_with("glow", "mono", "normal", "medium")

    def test_a_part_of_the_edge_push_passes_the_part_through_to_the_overlay(self):
        owner = _owner(glow_style="rupture")
        overlay = mock.Mock()
        owner._effect_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "right", 0.5, False, "end")
        overlay.push.assert_called_once_with("right", 0.5, False, part="end")

    def test_a_part_of_the_edge_push_passes_the_part_through_to_the_glow(self):
        owner = _owner(glow_style="glow", glow=mock.Mock())
        owner._effect_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "right", 0.5, False, "end")
        owner.glow.set_pressure.assert_called_once_with("right", 0.5, False, "end")

    def test_a_corner_push_reaches_the_overlay_with_its_corner(self):
        """sender.py names the corner in `part` (test_links.py's CornerTests), and the overlay
        plays the corner form from it."""
        owner = _owner(glow_style="rupture")
        overlay = mock.Mock()
        owner._effect_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "top", 1.0, True, "top_left")
        overlay.push.assert_called_once_with("top", 1.0, True, part="top_left")

    def test_a_crossing_carries_crossed_true_through_to_the_overlay(self):
        owner = _owner(glow_style="rupture")
        overlay = mock.Mock()
        owner._effect_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 1.0, True)
        overlay.push.assert_called_once_with("left", 1.0, True, part=None)

    def test_a_crossing_carries_crossed_true_through_to_the_glow(self):
        owner = _owner(glow_style="glow", glow=mock.Mock())
        owner._effect_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 1.0, True)
        owner.glow.set_pressure.assert_called_once_with("left", 1.0, True, None)

    def test_edge_glow_switched_off_in_settings_draws_nothing(self):
        owner = _owner(edge_glow=False, glow=mock.Mock())
        owner._effect_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 0.4, False)
        owner._effect_overlay.assert_not_called()
        owner.glow.set_pressure.assert_not_called()

    def test_a_closing_window_draws_nothing(self):
        owner = _owner(glow_style="rupture")
        owner._closing = True
        overlay = mock.Mock()
        owner._effect_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_pressure(owner, "left", 0.4, False)
        overlay.push.assert_not_called()


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class ArrivalMethodTest(unittest.TestCase):
    """Only a Mac has a notch: a PC below this one lands on its bottom edge as an edge crossing."""

    def test_a_mac_landing_on_the_bottom_edge_came_through_its_notch(self):
        self.assertEqual(kvm_bridge_win.arrival_method("bottom", "macos"), "notch")

    def test_a_pc_landing_on_the_bottom_edge_crossed_an_edge(self):
        self.assertEqual(kvm_bridge_win.arrival_method("bottom", "windows"), "edge")

    def test_an_unknown_driver_on_the_bottom_edge_crossed_an_edge(self):
        self.assertEqual(kvm_bridge_win.arrival_method("bottom", ""), "edge")

    def test_other_edges_are_edges_and_no_edge_is_a_switch(self):
        self.assertEqual(kvm_bridge_win.arrival_method("left", "macos"), "edge")
        self.assertEqual(kvm_bridge_win.arrival_method(None, "macos"), "switch")


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class ArrivalRoutingTest(unittest.TestCase):
    """`method` is decided before _on_arrival ever runs, by the inline lambdas that wire
    the responder's and sender.py's arrival_callback in WindowsApplication.__init__
    (kvm_bridge_win.py: "notch" if edge == "bottom" else "edge", or "switch" if edge is None, for
    the responder around line 286; "switch" if edge is None else "edge" for the sender around line
    312 -- the sender has no notch of its own, since only the Mac has one). Neither lambda is a
    separately callable unit, so it is not unit-tested on its own; these tests start from the
    method it would have produced and check where _on_arrival sends it."""

    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])

    def test_an_edge_crossing_arrival_reaches_the_effects_overlay_when_wanted(self):
        owner = _owner(glow_style="rupture")
        overlay = mock.Mock()
        owner._effect_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_arrival(owner, "edge", "left", 10.0, 20.0)
        expected = effect_overlay.logical_point(10.0, 20.0)
        overlay.arrive.assert_called_once_with("edge", "left", expected)

    def test_a_notch_crossing_arrival_reaches_the_effects_overlay_with_its_own_method(self):
        """The Mac's notch always lands on this PC's bottom edge (the receiver arrival lambda's
        own comment: "The Mac's notch crossing lands on this PC's bottom edge"), remapped to
        method "notch" before this point -- _on_arrival forwards the method name unchanged."""
        owner = _owner(glow_style="rupture")
        overlay = mock.Mock()
        owner._effect_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_arrival(owner, "notch", "bottom", 10.0, 20.0)
        expected = effect_overlay.logical_point(10.0, 20.0)
        overlay.arrive.assert_called_once_with("notch", "bottom", expected)

    def test_an_arrival_under_glow_only_calls_crossed_in_when_an_overlay_already_exists(self):
        """Glow and Beam are edge styles with no arrival. crossed_in only makes a waiting switch
        give way, so with no effects window yet there is nothing to tell (see the next test)."""
        owner = _owner(glow_style="glow")
        owner._effect_overlay = mock.Mock(return_value=None)
        owner.effects = mock.Mock()
        kvm_bridge_win.WindowsApplication._on_arrival(owner, "edge", "left", 10.0, 20.0)
        owner.effects.crossed_in.assert_called_once()

    def test_an_arrival_under_glow_with_no_overlay_ever_built_calls_nothing(self):
        owner = _owner(glow_style="glow")
        owner._effect_overlay = mock.Mock(return_value=None)
        owner.effects = None
        kvm_bridge_win.WindowsApplication._on_arrival(owner, "edge", "left", 10.0, 20.0)
        self.assertIsNone(owner.effects)

    def test_a_switch_arrival_with_no_edge_reaches_the_switch_overlay(self):
        owner = _owner(shortcut_arrival=True, shortcut_arrival_style="locator")
        overlay = mock.Mock()
        owner._switch_overlay = mock.Mock(return_value=overlay)
        kvm_bridge_win.WindowsApplication._on_arrival(owner, "switch", "", 10.0, 20.0)
        expected = effect_overlay.logical_point(10.0, 20.0)
        overlay.switched.assert_called_once_with(expected, "locator")

    def test_a_switch_arrival_does_nothing_when_the_locator_is_off(self):
        owner = _owner(shortcut_arrival=False)
        owner._switch_overlay = mock.Mock(return_value=None)
        kvm_bridge_win.WindowsApplication._on_arrival(owner, "switch", "", 10.0, 20.0)
        owner._switch_overlay.assert_called_once()


if __name__ == "__main__":
    unittest.main()
