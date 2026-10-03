"""Traces every crossing-effect "move" from bridge.py's engine feedback to the call that actually
reaches the effects overlay or a glow object, at the one hop that has no test today:
kvm_bridge_app.py's routing between controller.on_crossing / the driven-edge callbacks and
EffectsOverlay / EdgeGlow / NotchBeam / NotchIsland.

bridge.py raising each crossing kind ("pressure", "tick", "cross", "arrive", "home") with the
right Step fields (region, pin, via, mac_edge) is already proven by test_bridge.py's
CrossingWiringTests and core/tests/test_owner.py -- not repeated here.
This file starts from a Step already shaped the way bridge.py builds one, and checks where it
lands."""

import logging
import unittest
from types import SimpleNamespace
from unittest import mock
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from PyObjCTools import AppHelper

import crossing
import desktop_mac
from kvm_bridge_app import TrayApp
from core import effects


def _feel(**overrides):
    return dict(crossing.DEFAULT_CROSSING, **overrides)


class _FakeTrayApp:
    """Stand-in with just what the crossing-feedback and driven-edge methods read from self --
    same pattern as test_notify_user_thread.py's _FakeTrayApp: bind the real unbound methods onto
    a lightweight object instead of building a whole TrayApp, which needs a live NSStatusItem.
    The overlay and glow objects are plain Mocks, so
    these tests never touch effects.Player or draw anything."""

    _crossing_feedback_main = TrayApp._crossing_feedback_main
    _driven_pressure_main = TrayApp._driven_pressure_main
    _driven_arrival_main = TrayApp._driven_arrival_main
    _show_landing = TrayApp._show_landing
    crossing_feedback = TrayApp.crossing_feedback
    _driven_pressure = TrayApp._driven_pressure
    _driven_arrival = TrayApp._driven_arrival

    def __init__(self, feel):
        self.logger = logging.getLogger("test-effect-fires")
        self.controller = SimpleNamespace(cfg=SimpleNamespace(crossing=feel))
        self.effects_overlay = mock.Mock()
        self.effects_overlay.disabled = False
        self.edge_glow = mock.Mock()
        self.notch_beam = mock.Mock()
        self.notch_island = mock.Mock()
        self.haptics = mock.Mock()


class MainThreadHopTest(unittest.TestCase):
    """controller.on_crossing and the receiver's pressure/arrival callbacks fire off background
    threads (the event tap, the ack thread, the receiver's session thread); every one of them
    must hop to the main thread via AppHelper.callAfter before touching AppKit, the same
    requirement test_notify_user_thread.py proves for notify_user."""

    def test_crossing_feedback_hops_to_the_main_thread(self):
        app = _FakeTrayApp(_feel())
        step = crossing.Step(pressure=0.2)
        with mock.patch.object(AppHelper, "callAfter") as call_after:
            app.crossing_feedback("pressure", step)
        call_after.assert_called_once_with(app._crossing_feedback_main, "pressure", step)

    def test_driven_pressure_reads_the_cursor_then_hops_to_the_main_thread(self):
        app = _FakeTrayApp(_feel())
        with mock.patch.object(desktop_mac, "cursor_position", return_value=(5, 6)), mock.patch.object(
            AppHelper, "callAfter"
        ) as call_after:
            app._driven_pressure("left", 0.3, False)
        call_after.assert_called_once_with(app._driven_pressure_main, "left", 0.3, False, (5, 6), None)

    def test_driven_arrival_hops_to_the_main_thread(self):
        app = _FakeTrayApp(_feel())
        with mock.patch.object(AppHelper, "callAfter") as call_after:
            app._driven_arrival("left", 10.0, 20.0)
        call_after.assert_called_once_with(app._driven_arrival_main, "left", 10.0, 20.0)


class CrossingFeedbackDepartureTest(unittest.TestCase):
    """The Mac's own pressure/tick/cross Steps, as bridge.py's _handle_local_mouse builds them."""

    def test_pressure_reaches_the_effects_overlay_when_the_style_is_an_effect(self):
        app = _FakeTrayApp(_feel(glow_style="rupture"))
        app.effects_overlay.wanted.return_value = True
        step = crossing.Step(pressure=0.4, hold=True, pin=(1727.0, 558.0), mac_edge="right",
                              region=(1727.0, 0.0, 1.0, 1117.0), via="edge")
        app._crossing_feedback_main("pressure", step)
        app.effects_overlay.departure.assert_called_once_with("pressure", step)
        app.edge_glow.update.assert_not_called()
        app.notch_beam.update.assert_not_called()

    def test_pressure_reaches_edge_glow_when_the_style_is_glow(self):
        app = _FakeTrayApp(_feel(glow_style="glow"))
        app.effects_overlay.wanted.return_value = False
        step = crossing.Step(pressure=0.4, hold=True, pin=(1727.0, 558.0), mac_edge="right",
                              region=(1727.0, 0.0, 1.0, 1117.0), via="edge")
        app._crossing_feedback_main("pressure", step)
        app.edge_glow.update.assert_called_once_with("pressure", step)
        app.effects_overlay.departure.assert_not_called()

    def test_tick_at_the_notch_reaches_notch_beam_not_edge_glow(self):
        app = _FakeTrayApp(_feel(glow_style="beam", notch_style="beam"))
        app.effects_overlay.wanted.return_value = False
        step = crossing.Step(pressure=0.6, tick=True, pin=(700.0, 0.0), mac_edge="top",
                              region=(600.0, 0.0, 200.0, 1.0), via="notch")
        app._crossing_feedback_main("tick", step)
        app.notch_beam.update.assert_called_once_with("tick")
        app.edge_glow.update.assert_not_called()

    def test_tick_at_the_notch_reaches_notch_island_when_the_notch_style_is_island(self):
        app = _FakeTrayApp(_feel(glow_style="beam", notch_style="island"))
        app.effects_overlay.wanted.return_value = False
        step = crossing.Step(pressure=0.6, tick=True, pin=(700.0, 0.0), mac_edge="top",
                              region=(600.0, 0.0, 200.0, 1.0), via="notch")
        app._crossing_feedback_main("tick", step)
        app.notch_island.update.assert_called_once_with("tick")
        app.notch_beam.update.assert_not_called()

    def test_cross_reaches_the_effects_overlay_when_the_style_is_an_effect(self):
        app = _FakeTrayApp(_feel(glow_style="wormhole"))
        app.effects_overlay.wanted.return_value = True
        step = crossing.Step(pressure=1.0, crossed=True, pin=(1727.0, 558.0), edge="left", offset=0.5,
                              mac_edge="right", region=(1727.0, 0.0, 1.0, 1117.0), via="edge")
        app._crossing_feedback_main("cross", step)
        app.effects_overlay.departure.assert_called_once_with("cross", step)

    def test_cross_reaches_edge_glow_when_the_style_is_glow(self):
        app = _FakeTrayApp(_feel(glow_style="glow"))
        app.effects_overlay.wanted.return_value = False
        step = crossing.Step(pressure=1.0, crossed=True, pin=(1727.0, 558.0), edge="left", offset=0.5,
                              mac_edge="right", region=(1727.0, 0.0, 1.0, 1117.0), via="edge")
        app._crossing_feedback_main("cross", step)
        app.edge_glow.update.assert_called_once_with("cross", step)

    def test_glow_switched_off_in_settings_draws_nothing(self):
        app = _FakeTrayApp(_feel(glow=False, glow_style="glow"))
        app.effects_overlay.wanted.return_value = False
        step = crossing.Step(pressure=0.4, hold=True, pin=(1727.0, 558.0), mac_edge="right",
                              region=(1727.0, 0.0, 1.0, 1117.0), via="edge")
        app._crossing_feedback_main("pressure", step)
        app.edge_glow.update.assert_not_called()
        app.effects_overlay.departure.assert_not_called()

    def test_an_effect_that_disables_itself_this_event_falls_back_to_the_glow(self):
        """The comment above the glow branch in _crossing_feedback_main: an effect that failed on
        this very event has turned itself off, so today's glow draws instead and a breakthrough is
        never lost. effects_overlay.disabled flipping True is what the real overlay's _guard does
        on a drawing failure; simulated here by setting it directly on the mock."""
        app = _FakeTrayApp(_feel(glow_style="rupture"))
        app.effects_overlay.wanted.return_value = True
        app.effects_overlay.disabled = True
        step = crossing.Step(pressure=1.0, crossed=True, pin=(1727.0, 558.0), edge="left", offset=0.5,
                              mac_edge="right", region=(1727.0, 0.0, 1.0, 1117.0), via="edge")
        app._crossing_feedback_main("cross", step)
        app.effects_overlay.departure.assert_called_once_with("cross", step)
        app.edge_glow.update.assert_called_once_with("cross", step)


class CrossingFeedbackArrivalTest(unittest.TestCase):
    """The Mac's own "arrive" Step, as bridge.py's _handle_switch builds it when Windows' switch
    reply names a crossing edge (not a plain shortcut switch, which is "home", tested below)."""

    def test_a_crossing_arrival_reaches_the_effects_overlay_when_the_style_is_an_effect(self):
        app = _FakeTrayApp(_feel(glow_style="rupture"))
        app.effects_overlay.wanted.return_value = True
        step = crossing.Step(mac_edge="right", pin=(1727.0, 558.0))
        app._crossing_feedback_main("arrive", step)
        app.effects_overlay.arrival.assert_called_once_with("edge", 1727.0, 558.0, "right")
        app.effects_overlay.crossed_in.assert_not_called()

    def test_a_crossing_arrival_under_glow_calls_crossed_in_and_the_edge_glow(self):
        """Glow and Beam are edge styles with no arrival: EdgeGlow.update ignores "arrive", and
        crossed_in only makes a waiting switch arrival give way to the crossing."""
        app = _FakeTrayApp(_feel(glow_style="glow"))
        app.effects_overlay.wanted.return_value = False
        step = crossing.Step(mac_edge="right", pin=(1727.0, 558.0))
        app._crossing_feedback_main("arrive", step)
        app.effects_overlay.crossed_in.assert_called_once()
        app.effects_overlay.arrival.assert_not_called()
        app.edge_glow.update.assert_called_once_with("arrive", step)


class HomeArrivalTest(unittest.TestCase):
    """kind "home": input came back by the shortcut, the menu, or set_redirecting(False), and
    _crossing_feedback_main routes it through _show_landing rather than the glow/effect branch
    the other kinds share."""

    def test_home_shows_where_the_pointer_landed_when_the_switch_locator_is_on(self):
        app = _FakeTrayApp(_feel(shortcut_arrival=True))
        app.effects_overlay.wanted_switch.return_value = True
        step = crossing.Step(pin=(400.0, 300.0))
        app._crossing_feedback_main("home", step)
        app.effects_overlay.switched.assert_called_once_with(400.0, 300.0)

    def test_home_does_nothing_when_the_switch_locator_is_off(self):
        app = _FakeTrayApp(_feel(shortcut_arrival=False))
        app.effects_overlay.wanted_switch.return_value = False
        step = crossing.Step(pin=(400.0, 300.0))
        app._crossing_feedback_main("home", step)
        app.effects_overlay.switched.assert_not_called()


class DrivenReturnEdgeTest(unittest.TestCase):
    """The PC-driven return edge: LinkResponder's pressure_callback and arrival_callback, wired in
    kvm_bridge_app.py's TrayApp.__init__ as WindowsInput(pressure_callback=self._driven_pressure,
    arrival_callback=self._driven_arrival), land here after the main-thread hop proved above."""

    def test_a_return_push_reaches_the_effects_overlay_when_wanted(self):
        app = _FakeTrayApp(_feel(glow_style="rupture"))
        app.effects_overlay.wanted.return_value = True
        app._driven_pressure_main("left", 0.5, False, (10.0, 20.0))
        app.effects_overlay.return_push.assert_called_once_with("left", 0.5, False, (10.0, 20.0), None)

    def test_a_return_push_lights_the_edge_glow_under_glow(self):
        app = _FakeTrayApp(_feel(glow_style="glow"))
        app.effects_overlay.wanted.return_value = False
        app._driven_pressure_main("left", 0.5, False, (10.0, 20.0))
        app.effects_overlay.return_push.assert_not_called()
        app.edge_glow.driven.assert_called_once_with("left", 0.5, False, (10.0, 20.0), None)

    def test_a_way_home_through_a_corner_plays_its_corner_form(self):
        for style, wanted, target in (("rupture", True, "return_push"), ("glow", False, "driven")):
            app = _FakeTrayApp(_feel(glow_style=style))
            app.effects_overlay.wanted.return_value = wanted
            app._driven_pressure_main("top", 0.5, False, (10.0, 20.0), "top_left")
            owner = app.effects_overlay if wanted else app.edge_glow
            getattr(owner, target).assert_called_once_with("top", 0.5, False, (10.0, 20.0), "top_left")

    def test_a_third_of_the_edge_plays_as_the_edge(self):
        app = _FakeTrayApp(_feel(glow_style="glow"))
        app.effects_overlay.wanted.return_value = False
        app._driven_pressure_main("left", 0.5, False, (10.0, 20.0), "middle")
        app.edge_glow.driven.assert_called_once_with("left", 0.5, False, (10.0, 20.0), None)

    def test_a_driven_crossing_reaches_the_effects_overlay_when_wanted(self):
        app = _FakeTrayApp(_feel(glow_style="rupture"))
        app.effects_overlay.wanted.return_value = True
        app._driven_arrival_main("left", 10.0, 20.0)
        app.effects_overlay.arrival.assert_called_once_with("edge", 10.0, 20.0, "left")

    def test_a_driven_crossing_under_glow_only_calls_crossed_in(self):
        """Glow and Beam have no arrival; the crossing only makes a waiting switch give way."""
        app = _FakeTrayApp(_feel(glow_style="beam"))
        app.effects_overlay.wanted.return_value = False
        app._driven_arrival_main("left", 10.0, 20.0)
        app.effects_overlay.crossed_in.assert_called_once()
        app.effects_overlay.arrival.assert_not_called()
        app.edge_glow.update.assert_not_called()

    def test_a_driven_switch_with_no_edge_shows_where_the_pointer_landed(self):
        """edge=None is the receiver's _place_pointer telling the arrival callback the PC's input
        came home by a switch rather than a crossing -- routed through _show_landing exactly like
        the "home" kind above."""
        app = _FakeTrayApp(_feel(shortcut_arrival=True))
        app.effects_overlay.wanted_switch.return_value = True
        app._driven_arrival_main(None, 10.0, 20.0)
        app.effects_overlay.switched.assert_called_once_with(10.0, 20.0)


if __name__ == "__main__":
    unittest.main()


class EdgeGlowDrawTest(unittest.TestCase):
    """The real glow drawn through a push and its breakthrough, at an edge and in a corner, at every
    length: a failure here turns the glow off for the run, which no fake would show."""

    def test_a_push_and_its_breakthrough_draw_without_failing(self):
        import AppKit

        from kvm_bridge_app import EdgeGlow

        AppKit.NSApplication.sharedApplication()
        for style in ("glow", "beam"):
            for length in ("short", "normal", "long"):
                for region in ((1727.0, 0.0, 1.0, 1117.0), (1720.0, 1109.0, 8.0, 8.0)):
                    with self.subTest(style=style, length=length, region=region):
                        controller = SimpleNamespace(
                            cfg=SimpleNamespace(crossing=dict(crossing.DEFAULT_CROSSING, glow_style=style,
                                                              effect_length=length)),
                            crossing_pressure_now=lambda: 0.6,
                            _current_desktop_bounds=lambda: (0.0, 0.0, 1728.0, 1117.0),
                        )
                        glow = EdgeGlow(controller, logging.getLogger("test"))
                        glow.timer = mock.Mock()
                        step = crossing.Step(pressure=0.6, pin=(1727.0, 1116.0), mac_edge="right", region=region)
                        with mock.patch.object(desktop_mac, "monitors", return_value=[]):
                            glow.update("pressure", step)
                            glow.update("cross", step)
                            glow._draw()
                        self.assertFalse(glow.disabled)
                        glow._hide()


class MenuBarArmTest(unittest.TestCase):
    """Reported 28-09-2026: the Mac's Glow corner was lopsided, the top arm a faint stripe inside the
    menu bar beside a full side arm. Both arms measured the same 18.5pt on film, half the 37pt bar."""

    BOUNDS = (0.0, 0.0, 1728.0, 1117.0)

    def _glow(self, style, region, edge):
        import AppKit

        from kvm_bridge_app import EdgeGlow

        AppKit.NSApplication.sharedApplication()
        controller = SimpleNamespace(
            cfg=SimpleNamespace(crossing=dict(crossing.DEFAULT_CROSSING, glow_style=style)),
            crossing_pressure_now=lambda: 1.0,
            _current_desktop_bounds=lambda: self.BOUNDS,
        )
        glow = EdgeGlow(controller, logging.getLogger("test"))
        glow.timer = mock.Mock()
        glow._ensure_panel()
        # Already visible, so drawing never orders the panel onto the real screen.
        glow.visible = True
        glow.mac_edge, glow.region = edge, region
        return glow

    def _draw(self, glow, bar):
        with mock.patch.object(desktop_mac, "monitors", return_value=[]), \
                mock.patch("kvm_bridge_app.menu_bar_height", return_value=bar):
            glow._draw()
        self.assertFalse(glow.disabled)

    def test_a_top_corners_arm_fills_the_menu_bar_as_the_side_arm_fills_its_band(self):
        glow = self._glow("glow", (1720.0, 0.0, 8.0, 8.0), "right")
        self._draw(glow, 37.0)
        side = effects.edge_depth(1728, 1117, "medium")
        self.assertAlmostEqual(glow.fill.frame().size.height, 37.0)
        self.assertAlmostEqual(glow.side.frame().size.width, side)
        # Still hung from the top of the screen.
        self.assertAlmostEqual(glow.fill.frame().origin.y + 37.0, glow.CORNER_ARM)
        glow._hide()

    def test_a_bottom_corner_a_hidden_menu_bar_and_the_beam_are_unchanged(self):
        for style, region, bar in (("glow", (1720.0, 1109.0, 8.0, 8.0), 37.0),
                                   ("glow", (1720.0, 0.0, 8.0, 8.0), 0.0),
                                   ("beam", (1720.0, 0.0, 8.0, 8.0), 37.0)):
            with self.subTest(style=style, region=region, bar=bar):
                glow = self._glow(style, region, "right")
                self._draw(glow, bar)
                self.assertAlmostEqual(glow.fill.frame().size.height, glow.side.frame().size.width)
                glow._hide()

    def test_the_top_edge_fills_the_menu_bar_too(self):
        glow = self._glow("glow", (0.0, 0.0, 1728.0, 1.0), "top")
        self._draw(glow, 37.0)
        self.assertAlmostEqual(glow.panel.frame().size.height, 37.0)
        glow._hide()

    def test_the_menu_bar_is_read_from_the_screen_the_display_is(self):
        import kvm_bridge_app

        def rect(x, y, w, h):
            return SimpleNamespace(origin=SimpleNamespace(x=x, y=y), size=SimpleNamespace(width=w, height=h))

        built_in = SimpleNamespace(frame=lambda: rect(0, 0, 1728, 1117), visibleFrame=lambda: rect(0, 60, 1728, 1020))
        # Above the built-in display, its top 1080pt higher in AppKit and at -1080 in Quartz.
        external = SimpleNamespace(frame=lambda: rect(0, 1117, 1920, 1080), visibleFrame=lambda: rect(0, 1117, 1920, 1080))
        fake = SimpleNamespace(NSScreen=SimpleNamespace(screens=lambda: [built_in, external]))
        with mock.patch.object(kvm_bridge_app, "AppKit", fake):
            self.assertAlmostEqual(kvm_bridge_app.menu_bar_height((0.0, 0.0, 1728.0, 1117.0)), 37.0)
            self.assertAlmostEqual(kvm_bridge_app.menu_bar_height((0.0, -1080.0, 1920.0, 0.0)), 0.0)
            self.assertAlmostEqual(kvm_bridge_app.menu_bar_height((5000.0, 0.0, 6000.0, 900.0)), 0.0)
            self.assertAlmostEqual(kvm_bridge_app.menu_bar_height(None), 0.0)


class ThirdEndsTest(unittest.TestCase):
    def test_a_third_of_the_edge_fades_at_its_ends_and_the_whole_edge_does_not(self):
        from kvm_bridge_app import EdgeGlow

        controller = SimpleNamespace(cfg=SimpleNamespace(crossing=dict(crossing.DEFAULT_CROSSING)),
                                     _current_desktop_bounds=lambda: (0.0, 0.0, 1728.0, 1117.0))
        glow = EdgeGlow(controller, logging.getLogger("test"))
        glow.mac_edge = "right"
        with mock.patch.object(desktop_mac, "monitors", return_value=[]):
            glow.region = (1727.0, 372.0, 1.0, 373.0)
            self.assertAlmostEqual(glow._taper(), 72.0 / 373.0)
            glow.region = (1727.0, 0.0, 1.0, 1117.0)
            self.assertIsNone(glow._taper())
