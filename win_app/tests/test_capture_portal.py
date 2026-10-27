"""Wayland capture on fakes: the barriers that stand for each kind of zone, the press at a barrier
(push, slide, back into the screen), and the capture thread against a scripted InputCapture portal
and libei: a push that crosses, input while away, and every way home (the sender bringing it home,
a move back, a key at the edge, a pause, the desktop ending it, a stop, a dead connection, a
stalled thread)."""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import capture_portal
import desktop_portal
import portal
from core import return_edge
from core.return_edge import Rect
from portal_fakes import FakeBus, FakeDevice, FakeEi, vardict, wait_for

ONE = [Rect(0, 0, 1920, 1080)]
SESSION = "/org/freedesktop/portal/desktop/session/1_42/beamer2"


class CapturePortalNamesTests(unittest.TestCase):
    def test_input_method_names_share_the_evdev_mapping_and_cannot_be_triggers(self):
        self.assertEqual((capture_portal.VK_TO_NAME[122], capture_portal.VK_TO_NAME[123]), ("hangul", "hanja"))
        self.assertEqual((capture_portal.VK_TITLES[122], capture_portal.VK_TITLES[123]), ("Han/Yeong", "Hanja"))
        self.assertTrue({122, 123} <= capture_portal.NOT_TRIGGER_VKS)


class BarrierTest(unittest.TestCase):
    def test_a_scoped_wayland_zone_does_not_create_barriers_on_another_display(self):
        screens = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        model = return_edge.ReturnEdge("left", display="1")

        found = capture_portal.barriers([model], screens, display_map={"0": screens[0], "1": screens[1]})

        self.assertEqual(found, [],
                         "display 1's left seam is internal and cannot be captured")

    def test_a_display_key_selects_its_zone_by_geometry_side_by_side(self):
        screens = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        models = [return_edge.ReturnEdge("top", display="A")]
        self.assertEqual(capture_portal.barriers(models, list(reversed(screens)), display_map={"A": screens[0], "B": screens[1]}),
                         [("top", (0, 0, 1919, 0))])

    def test_a_display_key_selects_its_zone_by_geometry_when_stacked(self):
        screens = [Rect(0, 0, 1600, 900), Rect(200, 950, 1200, 700)]
        models = [return_edge.ReturnEdge("right", display="A")]
        self.assertEqual(capture_portal.barriers(models, list(reversed(screens)), display_map={"A": screens[0], "B": screens[1]}),
                         [("right", (1600, 0, 1600, 899))])

    def test_kde_places_a_selected_whole_edge_in_reconciled_zone_coordinates(self):
        qt_screens = [Rect(-1920, 0, 1920, 1080), Rect(0, 0, 2560, 1440)]
        zones = [Rect(0, 0, 1280, 720), Rect(-960, 0, 960, 540)]
        desktop_portal._screens = qt_screens
        desktop_portal.set_zones(zones)
        self.addCleanup(desktop_portal.set_zones, [])
        self.addCleanup(setattr, desktop_portal, "_screens", [])

        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("top", display="0")], zones,
                                                 whole_edges=True,
                                                 display_map=desktop_portal.display_map()),
                         [("top", (-960, 0, -1, 0))])

    def test_a_display_key_scopes_split_edges_and_corners(self):
        screens = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        display_map = {"A": screens[0], "B": screens[1]}
        self.assertEqual(capture_portal.barriers([return_edge.PartEdge("left", {"middle"}, display="A")], screens,
                                                 display_map=display_map),
                         [("left", (0, 360, 0, 719))])
        self.assertEqual(sorted(capture_portal.barriers([return_edge.CornerPush("top_right", "right", display="A")], screens,
                                                        display_map=display_map)),
                         [("top", (1911, 0, 1919, 0))])

    def test_an_unmatched_display_does_not_install_barriers_on_other_displays(self):
        screens = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        unmatched = []
        found = capture_portal.barriers([return_edge.ReturnEdge("top", display="missing")], screens,
                                        display_map={"A": screens[0], "B": screens[1]},
                                        warn_unmatched=unmatched.append)
        self.assertEqual(found, [])
        self.assertEqual(unmatched, ["missing"])

    def test_an_unmatched_display_is_logged_once(self):
        hooks = capture_portal.Hooks(None, None, None)
        with self.assertLogs(capture_portal.LOGGER, level="WARNING") as logged:
            hooks._warn_unmatched_display("missing")
            hooks._warn_unmatched_display("missing")
        self.assertEqual(len(logged.records), 1)

    def test_a_display_key_is_part_of_the_barrier_model_key(self):
        self.assertNotEqual(capture_portal.model_key([return_edge.ReturnEdge("top", display="A")]),
                            capture_portal.model_key([return_edge.ReturnEdge("top", display="B")]))

    def test_kde_uses_one_complete_edge_for_selected_thirds(self):
        models = [return_edge.PartEdge("left", {"middle"}), return_edge.PartEdge("left", {"end"})]
        self.assertEqual(capture_portal.barriers(models, ONE, whole_edges=True), [("left", (0, 0, 0, 1079))])

    def test_kde_corners_use_complete_edges_without_internal_seams(self):
        models = [return_edge.CornerPush("top_right", "right")]
        screens = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        self.assertCountEqual(capture_portal.barriers(models, screens, whole_edges=True),
                         [("top", (0, 0, 1919, 0)), ("right", (3840, 0, 3840, 1079)),
                          ("top", (1920, 0, 3839, 0))])

    def test_kde_does_not_widen_a_partly_exposed_side_across_a_monitor_seam(self):
        screens = [Rect(0, 0, 1920, 1080), Rect(1920, 200, 1280, 1024)]
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("right")], screens, whole_edges=True),
                         [("right", (3200, 200, 3200, 1223))])

    def test_a_whole_edge_on_one_display(self):
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("right")], ONE), [("right", (1920, 0, 1920, 1079))])
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("top")], ONE), [("top", (0, 0, 1919, 0))])

    def test_an_edge_between_two_displays_has_no_barrier(self):
        side_by_side = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("right")], side_by_side), [("right", (3840, 0, 3840, 1079))])
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("top")], side_by_side),
                         [("top", (0, 0, 1919, 0)), ("top", (1920, 0, 3839, 0))])

    def test_a_staggered_display_leaves_the_uncovered_stretch_of_its_neighbour(self):
        staggered = [Rect(0, 0, 1920, 1080), Rect(1920, 200, 1280, 1024)]
        self.assertEqual(capture_portal.barriers([return_edge.ReturnEdge("right")], staggered),
                         [("right", (1920, 0, 1920, 199)), ("right", (3200, 200, 3200, 1223))])

    def test_thirds_are_split_where_part_of_splits_them(self):
        found = capture_portal.barriers([return_edge.PartEdge("left", {"middle"})], ONE)
        self.assertEqual(found, [("left", (0, 360, 0, 719))])
        self.assertEqual(return_edge.part_of(359 / 1080), "start")
        self.assertEqual(return_edge.part_of(360 / 1080), "middle")
        self.assertEqual(return_edge.part_of(720 / 1080), "end")
        both = capture_portal.barriers([return_edge.PartEdge("bottom", {"start", "end"})], ONE)
        self.assertEqual(both, [("bottom", (0, 1080, 639, 1080)), ("bottom", (1280, 1080, 1919, 1080))])

    def test_a_corner_is_its_two_walls_as_far_as_the_corner_push_reaches(self):
        found = capture_portal.barriers([return_edge.CornerPush("top_right", "right")], ONE)
        self.assertEqual(sorted(found), [("right", (1920, 0, 1920, 8)), ("top", (1911, 0, 1919, 0))])
        model = return_edge.CornerPush("top_right", "right")
        self.assertTrue(model._at_edge(ONE, (1919, 8)) and model._at_edge(ONE, (1911, 0)))
        self.assertFalse(model._at_edge(ONE, (1919, 9)) or model._at_edge(ONE, (1910, 0)))

    def test_a_notch_stretch_has_none_and_a_repeat_is_placed_once(self):
        models = [return_edge.SpanEdge("top", lambda: (100, 200)), return_edge.ReturnEdge("left"), return_edge.ReturnEdge("left")]
        self.assertEqual(capture_portal.barriers(models, ONE), [("left", (0, 0, 0, 1079))])

    def test_mutter_accepts_every_barrier_on_the_laptops_logical_monitor(self):
        models = [return_edge.ReturnEdge(edge) for edge in ("left", "right", "top", "bottom")]
        models += [return_edge.PartEdge(edge, {part}) for edge in ("left", "right", "top", "bottom")
                   for part in ("start", "middle", "end")]
        models += [return_edge.CornerPush(corner, edge) for corner, edge in
                   (("top_left", "left"), ("top_right", "right"), ("bottom_left", "left"), ("bottom_right", "right"))]
        for model in models:
            found = [position for _wall, position in capture_portal.barriers([model], LAPTOP)]
            self.assertTrue(found, model)
            for position in found:
                self.assertTrue(mutter_accepts(position, LAPTOP), (model, position))
        self.assertFalse(mutter_accepts((0, 0, 3199, 0), LAPTOP))  # the panel's pixels, not its logical size
        self.assertFalse(mutter_accepts((1601, 0, 1601, 899), LAPTOP))  # a pixel past the edge
        self.assertFalse(mutter_accepts((800, 0, 800, 899), LAPTOP))  # across the display


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class PressTest(unittest.TestCase):
    def test_a_push_holds_a_slide_moves_along_and_a_move_back_lets_go_where_it_ends(self):
        clock = Clock()
        press = capture_portal.Press("right", (1920.0, 500.0), ONE, clock)
        self.assertEqual(press.point, (1919, 500))
        self.assertTrue(press.feed(6, 0))
        self.assertEqual(press.point, (1919, 500))
        self.assertTrue(press.feed(3, -10))
        self.assertEqual(press.point, (1919, 490))
        self.assertTrue(press.feed(0, 4))
        self.assertEqual(press.point, (1919, 494))
        self.assertFalse(press.feed(-5, 2))
        self.assertEqual(press.point, (1914, 496))

    def test_travel_past_the_display_is_not_kept(self):
        press = capture_portal.Press("right", (1920.0, 1075.0), ONE, Clock())
        press.feed(0, 100)
        self.assertEqual(press.point, (1919, 1079))
        press.feed(0, -10)
        self.assertEqual(press.point, (1919, 1069))

    def test_a_pause_in_the_push_is_idle(self):
        clock = Clock()
        press = capture_portal.Press("left", (0.0, 10.0), ONE, clock)
        press.feed(-4, 0)
        clock.now += capture_portal.IDLE_SECONDS / 2
        self.assertFalse(press.idle())
        press.feed(0, 3)  # a slide is not a push
        clock.now += capture_portal.IDLE_SECONDS
        self.assertTrue(press.idle())


class MemoryTokens:
    def __init__(self, token=""):
        self.token = token

    def read(self):
        return self.token

    def write(self, token):
        self.token = token


LAPTOP = [Rect(0, 0, 1600, 900)]  # a 3200x1800 panel at scale 2.0 in mutter's logical layout


def _adjacency(m, x1, y1, x2, y2):
    """mutter 50.0 get_barrier_adjacency (src/backends/meta-input-capture-session.c), line for line."""
    x_min, x_max, y_min, y_max = min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)
    if x1 == x2:
        if x1 < m.x or x1 > m.x + m.width or y_max < m.y or y_min >= m.y + m.height:
            return "none"
        if x1 in (m.x, m.x + m.width):
            return "partial" if y_max > m.y + m.height or y_min < m.y else "contained"
        return "overlap"
    if y1 == y2:
        if y1 < m.y or y1 > m.y + m.height or x_max < m.x or x_min >= m.x + m.width:
            return "none"
        if y1 in (m.y, m.y + m.height):
            return "partial" if x_max > m.x + m.width or x_min < m.x else "contained"
        return "overlap"
    return "none"


def mutter_accepts(barrier, monitors):
    """mutter 50.0 check_barrier over the logical monitors, the first being primary."""
    x1, y1, x2, y2 = barrier
    if (x1 != x2 and y1 != y2) or (x1 == x2 and y1 == y2):
        return False
    adjacent = False
    for m in monitors:
        found = _adjacency(m, x1, y1, x2, y2)
        if found in ("overlap", "partial") or found == "contained" and adjacent:
            return False
        adjacent = adjacent or found == "contained"
    if adjacent and y1 == y2:
        rightmost = monitors[0]
        while True:
            beside = [m for m in monitors if m.x == rightmost.x + rightmost.width
                      and m.y < rightmost.y + rightmost.height and rightmost.y < m.y + m.height]
            if not beside:
                break
            rightmost = beside[0]
        fake = Rect(rightmost.x + rightmost.width, rightmost.y, rightmost.width, rightmost.height)
        if _adjacency(fake, x1, y1, x2, y2) != "none":
            return False
    return adjacent


class GnomeBus(FakeBus):
    """The InputCapture portal as mutter 50 and xdg-desktop-portal-gnome 50 run it: Enable and every
    added barrier need a session that is not enabled; ClearBarriers works in any state; Disable needs
    an enabled session and sends no Disabled signal. `refused` collects each barrier id it refused,
    `wasted` each Enable or Disable mutter would have refused."""

    def __init__(self, monitors, **kwargs):
        super().__init__(**kwargs)
        self.monitors = monitors
        self.enabled = False
        self.refused = []
        self.wasted = []
        self.answers["SetPointerBarriers"] = self._barriers

    def _barriers(self, body):
        failed = []
        for item in body[2]:
            wanted = item["position"][1]
            if self.enabled or not mutter_accepts(wanted, self.monitors):
                failed.append(item["barrier_id"][1])
        self.refused.extend(failed)
        return 0, {"failed_barriers": failed}

    def call(self, path, interface, method, signature, body):
        # The frontend calls Enable and Disable on the backend without waiting, so mutter's
        # "Already enabled" and "Session not enabled" reach only the journal, never Beamer.
        if method in ("Enable", "Disable"):
            if self.enabled == (method == "Enable"):
                self.wasted.append(method)
            self.enabled = method == "Enable"
        return super().call(path, interface, method, signature, body)

    def monitors_change(self):
        """Mutter disables the session on a monitor change and says only ZonesChanged."""
        self.enabled = False
        self.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "ZonesChanged", (SESSION, {}))

    def disabled_signal(self):
        """Mutter 50 never sends Disabled; KWin does."""
        self.enabled = False
        self.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Disabled", (SESSION, {}))


class KdeBus(FakeBus):
    """The InputCapture portal as xdg-desktop-portal-kde and KWin run it: SetPointerBarriers on a
    session not disabled disables it first, KWin saying so with a Disabled signal that reaches the
    client after the request's Response and so after its own Enable; Enable on an enabled session
    and Disable (which the portal sends as an enable) are no-ops. `disableds` counts the signals."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.enabled = False
        self.disableds = 0

    def call(self, path, interface, method, signature, body):
        if method == "Enable":
            self.enabled = True
        result = super().call(path, interface, method, signature, body)
        if method == "SetPointerBarriers" and self.enabled:
            self.enabled = False
            self.disableds += 1
            self.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Disabled", (SESSION, {}))
        return result


class Harness:
    """A Hooks on a fake portal, its callbacks recorded, and a sender stand-in that crosses after
    `push_to_cross` pushes, as the zone model does. `gnome` makes the portal GNOME's, on the zone
    the laptop reports."""

    def __init__(self, version=1, models=None, push_to_cross=2, tokens=None, gnome=False, kde=False, clock=time.monotonic):
        if gnome:
            self.bus = GnomeBus(LAPTOP, versions={capture_portal.INTERFACE: version})
        else:
            self.bus = (KdeBus if kde else FakeBus)(versions={capture_portal.INTERFACE: version})
            self.bus.answers["SetPointerBarriers"] = (0, {"failed_barriers": []})
        self.bus.answers["CreateSession"] = (0, {"session_handle": SESSION, "capabilities": 3})
        self.bus.answers["GetZones"] = (0, {"zones": [(1600, 900, 0, 0)] if gnome else [(1920, 1080, 0, 0)], "zone_set": 7})
        self.bus.returns["CreateSession2"] = (vardict({"session_handle": SESSION}),)
        self.bus.returns["ConnectToEIS"] = (99,)
        self.ei = FakeEi()
        self.keys, self.mice, self.motions, self.homes, self.failures = [], [], [], [], []
        self.away = False
        self.models = models if models is not None else [return_edge.ReturnEdge("right")]
        self.push_to_cross = push_to_cross
        self.tokens = tokens or MemoryTokens()
        self.hooks = capture_portal.Hooks(self._key, self._mouse, self._motion, connect=lambda: self.bus,
                                          ei=lambda fd: self.ei, tokens=self.tokens, clock=clock)
        self.hooks.grab_while(lambda: self.away)
        self.hooks.barriers_from(lambda: self.models)
        self.hooks.on_home = lambda: self.homes.append(True) or setattr(self, "away", False)
        self.hooks.on_failure = self.failures.append
        self.pointer = FakeDevice("captured relative pointer", {1, 16, 32})
        self.keyboard = FakeDevice("captured keyboard", {4})

    def _key(self, name, down, vk, us):
        self.keys.append((name, down, vk))
        return True

    def _mouse(self, message, x, y, data):
        self.mice.append((message, data))
        return True

    def _motion(self, dx, dy):
        self.motions.append((dx, dy))
        if len(self.motions) >= self.push_to_cross:
            self.away = True

    def start(self):
        self.hooks.start()
        wait_for(lambda: self.bus.made("Enable"), what="the session to be enabled")

    def activate(self, x=1920.0, y=500.0, barrier=1, activation=3):
        # libei's start sequence is the portal's activation id.
        self.ei.feed(("start", self.pointer, activation), ("start", self.keyboard, activation))
        self.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                      (SESSION, vardict({"activation_id": activation, "cursor_position": (x, y), "barrier_id": barrier})))
        wait_for(lambda: self.hooks.holding, what="the capture to begin")

    def cross(self):
        self.activate()
        self.ei.feed(*[("motion", self.pointer, 10.0, 0.0)] * self.push_to_cross)
        wait_for(lambda: self.away and self.hooks._away, what="input to go away")

    def releases(self):
        return [(body[1]["activation_id"][1], body[1]["cursor_position"][1]) for body in self.bus.made("Release")]


@unittest.skipIf(sys.platform == "win32", "the Wayland back ends select on pipes, which Windows cannot; they run only on Linux")
class HooksTest(unittest.TestCase):
    def setUp(self):
        desktop_portal._position = None
        desktop_portal._zones = []
        desktop_portal._regions = []
        desktop_portal._captured = False
        self.harness = None

    def tearDown(self):
        if self.harness is not None:
            self.harness.hooks.stop()

    def make(self, **kwargs):
        self.harness = Harness(**kwargs)
        return self.harness

    def test_kde_releases_an_unselected_third_without_forwarding_input(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(models=[return_edge.PartEdge("right", {"middle"})])
            h.start()
            h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                       (SESSION, vardict({"activation_id": 3, "cursor_position": (1940.0, 100.0)})))
            wait_for(lambda: h.releases(), what="unselected KDE edge to be released")
            self.assertEqual(h.releases(), [(3, (1919.0, 100.0))])
            self.assertEqual((h.keys, h.mice, h.motions), ([], [], []))
            self.assertFalse(h.hooks.holding)

    def test_kde_selected_third_accepts_overshoot_and_a_missing_barrier_id(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(models=[return_edge.PartEdge("right", {"middle"})])
            h.start()
            placed = h.bus.made("SetPointerBarriers")[-1][2]
            self.assertEqual(placed[0]["position"], ("(iiii)", (1920, 0, 1920, 1079)))
            h.activate(x=1940.0, y=500.0, barrier=0)
            self.assertTrue(h.hooks.holding)
            self.assertEqual(h.hooks._press.wall, "right")

    def test_rejected_kde_sequence_drops_early_and_late_input_but_keeps_a_new_sequence(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(models=[return_edge.PartEdge("right", {"middle"})])
            h.start()
            hooks = h.hooks
            hooks._ei_event(("start", h.pointer, 3))
            hooks._ei_event(("motion", h.pointer, 10.0, 0.0))
            hooks._activated({"activation_id": 3, "cursor_position": (1920.0, 100.0)})
            hooks._ei_event(("motion", h.pointer, 10.0, 0.0))
            hooks._ei_event(("start", h.keyboard, 3))
            hooks._ei_event(("key", h.keyboard, 30, True))
            self.assertEqual(hooks._early, [])
            self.assertEqual((h.keys, h.mice, h.motions), ([], [], []))
            hooks._ei_event(("stop", h.pointer))
            self.assertIn(3, hooks._rejected)
            hooks._ei_event(("stop", h.keyboard))
            self.assertNotIn(3, hooks._rejected)
            hooks._ei_event(("start", h.pointer, 4))
            hooks._ei_event(("motion", h.pointer, 10.0, 0.0))
            hooks._activated({"activation_id": 4, "cursor_position": (1920.0, 500.0)})
            self.assertEqual(h.motions, [(10.0, 0.0)])

    def test_kde_selection_checks_the_actual_monitor_edge(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(models=[return_edge.PartEdge("right", {"middle"})])
            h.start()
            h.hooks._placed = [("right", (1920, 0, 1920, 1079)), ("right", (4000, 0, 4000, 1079))]
            h.hooks._walls = {1: "right", 2: "right"}
            h.hooks._selected = [("right", (1920, 360, 1920, 719))]
            self.assertTrue(h.hooks._selected_at("right", (1940, 500)))
            self.assertFalse(h.hooks._selected_at("right", (4000, 500)))

    def test_kde_does_not_clamp_along_edge_overshoot_into_a_selected_end_third(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(models=[return_edge.PartEdge("right", {"end"})])
            h.start()
            self.assertFalse(h.hooks._selected_at("right", (1940, 2000)))

    def test_kde_rejection_without_an_activation_id_drops_a_late_start(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(models=[return_edge.PartEdge("right", {"middle"})])
            h.start()
            h.hooks._activated({"cursor_position": (1920.0, 100.0)})
            h.hooks._ei_event(("start", h.pointer, 3))
            h.hooks._ei_event(("motion", h.pointer, 10.0, 0.0))
            self.assertEqual(h.hooks._early, [])
            h.hooks._ei_event(("stop", h.pointer))
            h.hooks._activated({"activation_id": 4, "cursor_position": (1920.0, 500.0)})
            h.hooks._ei_event(("start", h.pointer, 4))
            h.hooks._ei_event(("motion", h.pointer, 10.0, 0.0))
            self.assertEqual(h.motions, [(10.0, 0.0)])

    def test_kde_missing_barrier_id_allows_large_normal_axis_overshoot(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(models=[return_edge.PartEdge("right", {"middle"})])
            h.start()
            self.assertEqual(h.hooks._wall_at((2420, 500)), "right")

    def test_the_barriers_go_on_the_zones_and_the_session_is_enabled(self):
        h = self.make()
        h.start()
        listed = h.bus.made("SetPointerBarriers")[0][2]
        self.assertEqual([item["position"] for item in listed], [("(iiii)", (1920, 0, 1920, 1079))])
        self.assertEqual(h.bus.made("SetPointerBarriers")[0][3], 7)
        self.assertEqual(desktop_portal.monitors(), ONE)

    def test_the_session_says_once_that_it_is_ready(self):
        with self.assertLogs(capture_portal.LOGGER, level="INFO") as said:
            h = self.make()
            h.start()
            h.models = [return_edge.ReturnEdge("bottom")]
            wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the session enabled again")
        ready = [line for line in said.output if "Input capture is ready" in line]
        self.assertEqual(ready, ["INFO:capture_portal:Input capture is ready: 1 of 1 edge barriers placed"])
        self.assertEqual(
            [[item["position"] for item in call[2]] for call in h.bus.made("SetPointerBarriers")],
            [[("(iiii)", (1920, 0, 1920, 1079))], [("(iiii)", (0, 1080, 1919, 1080))]],
        )
        self.assertEqual(h.failures, [])

    def test_push_through_moves_to_a_barrier_then_steps_outward_and_captures_once(self):
        h = self.make()
        h.start()
        captured, failed, moves = [], [], []
        with mock.patch.object(capture_portal, "inject_portal", create=True) as injection:
            injection.push.side_effect = lambda x, y, deltas, wanted: (
                moves.append(("to", (x, y))) if wanted() else None,
                moves.extend(("by", delta) for delta in deltas) if wanted() else None,
            )
            self.assertTrue(h.hooks.push_through(h.models, lambda: captured.append(True), lambda: failed.append(True)))
        self.assertEqual(moves, [("to", (1916, 539))] + [("by", (24, 0))] * 6)
        h.activate(x=1920.0, y=539.0)
        self.assertEqual((captured, failed), ([True], []))

    def test_a_queued_push_is_discarded_after_its_capture_deadline(self):
        clock = Clock()
        h = self.make(clock=clock)
        h.start()
        queued, moves, failed = [], [], []
        with mock.patch.object(capture_portal, "inject_portal", create=True) as injection:
            injection.push.side_effect = lambda x, y, deltas, wanted: queued.append((x, y, deltas, wanted))
            self.assertTrue(h.hooks.push_through(h.models, lambda: None, lambda: failed.append(True)))
            clock.now += capture_portal.PUSH_SECONDS + 0.01
            h.hooks._tick()
            x, y, deltas, wanted = queued[0]
            if wanted():
                moves.append(("to", (x, y)))
                moves.extend(("by", delta) for delta in deltas)
        self.assertEqual(failed, [True])
        self.assertEqual(moves, [])

    def test_a_queued_push_run_after_its_deadline_does_nothing_before_the_tick(self):
        clock = Clock()
        h = self.make(clock=clock)
        h.start()
        queued = []
        with mock.patch.object(capture_portal, "inject_portal", create=True) as injection:
            injection.push.side_effect = lambda x, y, deltas, wanted: queued.append(wanted)
            self.assertTrue(h.hooks.push_through(h.models, lambda: None, lambda: None))
        self.assertTrue(queued[0]())
        clock.now += capture_portal.PUSH_SECONDS + 0.01
        self.assertFalse(queued[0]())

    def test_push_through_ignores_an_activation_at_another_wall(self):
        h = self.make()
        h.start()
        captured, failed = [], []
        with mock.patch.object(capture_portal, "inject_portal", create=True):
            self.assertTrue(h.hooks.push_through(h.models, lambda: captured.append(True), lambda: failed.append(True)))
        h.hooks._activated({"activation_id": 9, "cursor_position": (0, 500), "barrier_id": 2})
        self.assertEqual((captured, failed), ([], []))

    def test_push_through_deadline_calls_failure_from_tick_once(self):
        clock = Clock()
        h = self.make(clock=clock)
        h.start()
        failed = []
        with mock.patch.object(capture_portal, "inject_portal", create=True):
            self.assertTrue(h.hooks.push_through(h.models, lambda: None, lambda: failed.append(True)))
        clock.now += capture_portal.PUSH_SECONDS + 0.01
        h.hooks._tick()
        h.hooks._tick()
        self.assertEqual(failed, [True])

    def test_push_through_refuses_when_there_is_no_session_or_no_barrier(self):
        h = self.make()
        with mock.patch.object(capture_portal, "inject_portal", create=True) as injection:
            self.assertFalse(h.hooks.push_through([], lambda: None, lambda: None))
            h.start()
            self.assertFalse(h.hooks.push_through([], lambda: None, lambda: None))
            injection.push.assert_not_called()

    def test_push_through_refuses_while_input_is_captured(self):
        h = self.make()
        h.start()
        h.hooks._activation = 3
        with mock.patch.object(capture_portal, "inject_portal", create=True) as injection:
            self.assertFalse(h.hooks.push_through(h.models, lambda: None, lambda: None))
            injection.push.assert_not_called()

    def test_push_through_refuses_a_second_request_while_one_is_pending(self):
        h = self.make()
        h.start()
        with mock.patch.object(capture_portal, "inject_portal", create=True) as injection:
            self.assertTrue(h.hooks.push_through(h.models, lambda: None, lambda: None))
            self.assertFalse(h.hooks.push_through(h.models, lambda: None, lambda: None))
            injection.push.assert_called_once()
            x, y, moves, _wanted = injection.push.call_args.args
            self.assertEqual((x, y), (1916, 539))
            self.assertEqual(moves, [(24, 0)] * capture_portal.PUSH_STEPS)

    def test_kde_push_starts_inside_the_last_pixel_column_barrier(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE"}):
            h = self.make(kde=True)
            h.start()
            moves = []
            with mock.patch.object(capture_portal, "inject_portal", create=True) as injection:
                injection.push.side_effect = lambda x, y, deltas, wanted: (
                    moves.append(("to", (x, y))) if wanted() else None,
                    moves.extend(("by", delta) for delta in deltas) if wanted() else None,
                )
                self.assertTrue(h.hooks.push_through(h.models, lambda: None, lambda: None))
            self.assertEqual(h.hooks._selected[0], ("right", (1920, 0, 1920, 1079)))
            self.assertEqual(moves[0], ("to", (1916, 539)))

    def test_a_push_crosses_input_goes_away_and_comes_home_where_the_sender_lands_it(self):
        h = self.make()
        h.start()
        h.cross()
        self.assertEqual(h.motions, [(10, 0), (10, 0)])
        self.assertEqual(desktop_portal.position(), (1919, 500))
        h.ei.feed(("key", h.keyboard, 1, True), ("frame", h.keyboard, 0), ("key", h.keyboard, 1, False), ("frame", h.keyboard, 0),
                  ("button", h.pointer, 0x110, True), ("scroll_discrete", h.pointer, 0, 120), ("frame", h.pointer, 0),
                  ("motion", h.pointer, 2.5, -1.5), ("motion", h.pointer, 0.5, 0.5), ("frame", h.pointer, 0))
        wait_for(lambda: len(h.motions) >= 3 and len(h.keys) == 2, what="the input away")
        self.assertEqual(h.keys, [("esc", True, 1), ("esc", False, 1)])
        self.assertEqual(h.mice, [(capture_portal._BUTTONS[1][0], 0),
                                  (capture_portal.WM_MOUSEWHEEL, capture_portal._wheel_data(-capture_portal.WHEEL_DELTA))])
        self.assertEqual(h.motions[2:], [(2, -1), (1, 0)])
        self.assertEqual(h.releases(), [])
        desktop_portal.set_cursor_position(0, 300)
        h.away = False
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(3, (0.0, 300.0))])
        self.assertFalse(h.hooks.holding)

    def test_a_let_go_waits_a_moment_for_the_landing_the_sender_reports_after_input_comes_home(self):
        h = self.make()
        h.start()
        h.cross()
        h.away = False
        time.sleep(capture_portal.LANDING_SECONDS / 3)
        desktop_portal.set_cursor_position(0, 777)
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(3, (0.0, 777.0))])

    def test_a_paused_keyboard_releases_keys_held_by_that_device(self):
        h = self.make()
        h.start()
        h.cross()
        h.ei.feed(("key", h.keyboard, 29, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.keys, what="the held key to be forwarded")
        h.ei.feed(("device_paused", h.keyboard))
        wait_for(lambda: len(h.keys) == 2, what="the paused device's held key to be released")
        self.assertEqual([(name, down) for name, down, _vk in h.keys], [("cmd", True), ("cmd", False)])
        self.assertEqual(h.hooks._names_down, {})

    def test_moving_back_into_the_screen_lets_go_there_and_sends_nothing(self):
        h = self.make()
        h.start()
        h.cross()
        h.ei.feed(("key", h.keyboard, 29, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.keys, what="the held key to be forwarded")
        h.away = False
        desktop_portal.set_cursor_position(0, 502)
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(3, (0.0, 502.0))])
        self.assertFalse(h.hooks.holding)
        self.assertEqual(h.hooks._names_down, {})

    def test_a_key_at_the_edge_lets_go_and_reaches_nobody(self):
        h = self.make()
        h.start()
        h.activate()
        h.ei.feed(("key", h.keyboard, 30, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.keys, [])

    def test_a_pause_in_the_push_lets_go_where_it_rests(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.activate(y=40.0)
        h.ei.feed(("motion", h.pointer, 4.0, 0.0))
        wait_for(lambda: h.releases(), timeout=capture_portal.IDLE_SECONDS + 1, what="the let-go")
        self.assertEqual(h.releases(), [(3, (1919.0, 40.0))])
        self.assertEqual(h.motions, [(4, 0)])

    def test_events_ahead_of_the_activation_are_read_once_it_says_which_barrier(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 5), ("motion", h.pointer, 10.0, 0.0))
        time.sleep(0.15)
        self.assertEqual(h.motions, [])
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 5, "cursor_position": (1920.0, 9.0), "barrier_id": 1})))
        wait_for(lambda: h.motions, what="the early push")
        self.assertEqual(h.motions, [(10, 0)])

    def test_what_arrives_after_a_let_go_is_not_replayed_into_the_next_capture(self):
        h = self.make()
        h.start()
        h.activate()
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.releases(), what="the let-go")
        h.ei.feed(("motion", h.pointer, 7.0, 0.0), ("stop", h.pointer), ("stop", h.keyboard))
        time.sleep(0.15)
        h.activate(activation=4)
        time.sleep(0.15)
        self.assertEqual(h.motions, [])

    def test_a_barrier_it_does_not_know_is_let_go_at_once(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 1))
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 8, "cursor_position": (1920.0, 9.0), "barrier_id": 77})))
        wait_for(lambda: h.releases(), what="the let-go")
        self.assertEqual(h.releases(), [(8, (1919.0, 9.0))])

    def test_the_desktop_ending_a_capture_while_input_is_away_brings_it_home(self):
        h = self.make()
        h.start()
        h.cross()
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Deactivated",
                   (SESSION, vardict({"activation_id": 3, "cursor_position": (1000.0, 10.0)})))
        wait_for(lambda: h.homes, what="input home")
        self.assertFalse(h.hooks.holding)
        self.assertEqual(desktop_portal.position(), (1000, 10))
        self.assertEqual(h.releases(), [])

    def test_disabled_sets_the_barriers_again_and_enables_the_session(self):
        h = self.make()
        h.start()
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Disabled", (SESSION, {}))
        wait_for(lambda: len(h.bus.made("Enable")) >= 2, what="the session enabled again")
        self.assertEqual(len(h.bus.made("SetPointerBarriers")), 2)

    def test_held_edges_take_the_barriers_away_and_their_return_puts_them_back(self):
        h = self.make()
        h.start()
        models = h.models
        h.models = []
        wait_for(lambda: len(h.bus.made("SetPointerBarriers")) == 2, what="the barriers removed")
        self.assertEqual(h.bus.made("SetPointerBarriers")[1][2], [])
        enables = len(h.bus.made("Enable"))
        h.models = models
        wait_for(lambda: len(h.bus.made("SetPointerBarriers")) == 3, what="the barriers back")
        wait_for(lambda: len(h.bus.made("Enable")) == enables + 1, what="enabled again")

    def test_gnome_barriers_replaced_on_an_enabled_session_disable_it_first_and_none_is_refused(self):
        h = self.make(gnome=True, models=[return_edge.ReturnEdge("bottom")])
        h.start()
        self.assertEqual(h.bus.made("SetPointerBarriers")[0][2][0]["position"], ("(iiii)", (0, 900, 1599, 900)))
        h.models = [return_edge.ReturnEdge("bottom"), return_edge.ReturnEdge("left")]
        wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the session enabled again")
        names = [method for _interface, method, _body in h.bus.calls if method in ("Enable", "Disable", "SetPointerBarriers")]
        self.assertEqual(names, ["SetPointerBarriers", "Enable", "Disable", "SetPointerBarriers", "Enable"])
        self.assertEqual(len(h.bus.made("SetPointerBarriers")[1][2]), 2)
        self.assertEqual((h.bus.refused, h.bus.wasted), ([], []))
        self.assertTrue(h.bus.enabled and h.hooks._session.enabled)
        self.assertEqual(h.failures, [])

    def test_gnome_held_edges_and_their_return_leave_the_barriers_standing(self):
        h = self.make(gnome=True, models=[return_edge.ReturnEdge("bottom"), return_edge.ReturnEdge("left")])
        h.start()
        models = h.models
        h.models = []
        wait_for(lambda: len(h.bus.made("SetPointerBarriers")) == 2, what="the barriers removed")
        self.assertEqual(h.bus.made("SetPointerBarriers")[1][2], [])
        h.models = models
        wait_for(lambda: len(h.bus.made("Enable")) == 2, what="enabled again")
        self.assertEqual(len(h.bus.made("SetPointerBarriers")[2][2]), 2)
        self.assertEqual(len(h.bus.made("Disable")), 1)
        self.assertEqual(h.bus.refused, [])
        self.assertTrue(h.bus.enabled)
        self.assertEqual(h.failures, [])

    def test_after_a_monitor_change_the_barriers_are_set_without_a_disable(self):
        h = self.make(gnome=True)
        h.start()
        h.bus.monitors_change()
        wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the session enabled again")
        self.assertEqual(len(h.bus.made("SetPointerBarriers")), 2)
        self.assertEqual(h.bus.made("Disable"), [])
        self.assertEqual((h.bus.refused, h.bus.wasted), ([], []))
        self.assertTrue(h.bus.enabled)
        self.assertEqual(h.failures, [])

    def test_after_a_disabled_signal_the_barriers_are_set_without_a_disable(self):
        h = self.make(gnome=True)
        h.start()
        h.bus.disabled_signal()
        wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the session enabled again")
        self.assertEqual(h.bus.made("Disable"), [])
        self.assertEqual((h.bus.refused, h.bus.wasted), ([], []))
        self.assertEqual(h.failures, [])

    def test_kwins_disabled_after_every_set_of_barriers_does_not_set_them_again_and_again(self):
        with mock.patch.object(capture_portal, "DISABLED_SECONDS", 0.1):
            h = self.make(kde=True)
            h.start()
            h.models = [return_edge.ReturnEdge("right"), return_edge.ReturnEdge("bottom")]
            wait_for(lambda: h.bus.disableds == 1, what="the changed barriers placed on the enabled session")
            time.sleep(0.8)
            self.assertEqual(len(h.bus.made("SetPointerBarriers")), 2)
            self.assertEqual(h.bus.disableds, 1)
            self.assertTrue(h.bus.enabled)
            self.assertLessEqual(len(h.bus.made("Enable")), 4)
            self.assertEqual(h.failures, [])

    def devices(self, h):
        h.ei.feed(("device_added", h.pointer), ("device_added", h.keyboard))
        time.sleep(0.15)

    def cancel(self, h):
        """Mutter's cancel shortcut: the session back in init, its input devices removed, no signal."""
        h.bus.enabled = False
        h.ei.feed(("device_removed", h.pointer), ("device_removed", h.keyboard))

    def test_gnomes_cancel_shortcut_enables_the_session_again_without_setting_the_barriers(self):
        with mock.patch.object(capture_portal, "DISABLED_SECONDS", 0.1):
            h = self.make(gnome=True)
            h.start()
            self.devices(h)
            self.cancel(h)
            wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the session enabled again")
            time.sleep(0.4)
            self.assertEqual(len(h.bus.made("SetPointerBarriers")), 1)
            self.assertEqual(h.bus.made("Disable"), [])
            self.assertEqual(len(h.bus.made("Enable")), 2)
            self.assertTrue(h.bus.enabled)
            self.assertEqual((h.bus.refused, h.bus.wasted), ([], []))

    def test_the_cancel_shortcut_while_input_is_captured_ends_it_and_the_edges_come_back(self):
        with mock.patch.object(capture_portal, "DISABLED_SECONDS", 0.1):
            h = self.make(gnome=True)
            h.start()
            self.devices(h)
            h.activate(x=1600.0, y=450.0)
            h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Deactivated",
                       (SESSION, vardict({"activation_id": 3, "cursor_position": (800.0, 450.0)})))
            wait_for(lambda: not h.hooks.holding, what="the capture to end")
            time.sleep(0.3)
            self.assertEqual(len(h.bus.made("Enable")), 1)  # an ordinary end leaves the session as it is
            self.cancel(h)
            wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the session enabled again")
            self.assertTrue(h.bus.enabled)
            self.assertEqual(len(h.bus.made("SetPointerBarriers")), 1)
            self.assertEqual((h.bus.refused, h.bus.wasted), ([], []))
            self.assertEqual(h.failures, [])

    def test_the_devices_a_disable_of_its_own_removes_are_not_taken_for_a_cancel(self):
        with mock.patch.object(capture_portal, "DISABLED_SECONDS", 0.1):
            h = self.make(gnome=True, models=[return_edge.ReturnEdge("bottom")])
            h.start()
            self.devices(h)
            h.models = [return_edge.ReturnEdge("bottom"), return_edge.ReturnEdge("left")]
            wait_for(lambda: len(h.bus.made("Disable")) == 1, what="the session disabled")
            h.ei.feed(("device_removed", h.pointer), ("device_removed", h.keyboard))
            wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the session enabled with the new barriers")
            time.sleep(0.5)
            self.assertEqual(len(h.bus.made("Enable")), 2)
            self.assertEqual(len(h.bus.made("SetPointerBarriers")), 2)
            self.assertEqual((h.bus.refused, h.bus.wasted), ([], []))

    def test_a_disable_the_portal_refuses_is_logged_and_the_barriers_are_still_set(self):
        h = self.make(models=[return_edge.ReturnEdge("bottom")])
        h.start()
        h.bus.failures["Disable"] = portal.PortalError("Disable: Failed")
        with self.assertLogs(capture_portal.LOGGER, level="WARNING") as logs:
            h.models = [return_edge.ReturnEdge("bottom"), return_edge.ReturnEdge("left")]
            wait_for(lambda: len(h.bus.made("Enable")) == 2, what="the barriers set again")
        self.assertEqual(len(h.bus.made("SetPointerBarriers")), 2)
        self.assertIn("Could not disable", "\n".join(logs.output))
        self.assertEqual(h.failures, [])

    def test_a_stop_while_input_is_away_lets_go_and_ends_the_connection(self):
        h = self.make()
        h.start()
        h.cross()
        h.hooks.stop()
        self.assertEqual(len(h.releases()), 1)
        self.assertTrue(h.bus.closed and h.ei.closed)
        self.assertFalse(h.hooks.holding)

    def test_a_dead_input_connection_lets_go_ends_the_session_and_says_why(self):
        h = self.make()
        h.start()
        h.cross()
        h.ei.feed(("disconnect",))
        wait_for(lambda: h.failures, what="the failure")
        self.assertIn("stopped letting Beamer capture", h.failures[0].sentence)
        wait_for(lambda: h.bus.closed, what="the connection closed")
        self.assertEqual(len(h.releases()), 1)

    def test_a_stalled_thread_holding_input_has_its_connection_ended(self):
        saved = capture_portal.STALL_SECONDS
        capture_portal.STALL_SECONDS = 0.3
        try:
            h = self.make()
            h.start()
            h.activate()
            stalled = h.hooks._on_motion

            def stall(dx, dy):
                time.sleep(1.0)
                stalled(dx, dy)

            h.hooks._on_motion = stall
            h.ei.feed(("motion", h.pointer, 10.0, 0.0))
            wait_for(lambda: h.bus.aborted, timeout=2, what="the watchdog")
        finally:
            capture_portal.STALL_SECONDS = saved

    def test_a_refusal_is_said_in_the_apps_words_after_start_returns(self):
        h = self.make()
        h.bus.answers["CreateSession"] = (1, {})
        h.hooks.start()
        wait_for(lambda: h.failures, what="the refusal")
        self.assertEqual(h.failures[0].sentence, capture_portal.REFUSED)

    def test_an_input_capture_portal_reporting_zero_uses_v1_session_creation(self):
        h = self.make(version=0)
        with self.assertLogs(capture_portal.LOGGER, level="INFO") as logs:
            h.start()
        self.assertEqual(len(h.bus.made("CreateSession")), 1)
        self.assertEqual(h.bus.made("CreateSession2"), [])
        self.assertIn("Requesting InputCapture session", "\n".join(logs.output))

    def test_a_zero_version_input_capture_portal_reports_its_create_error(self):
        h = self.make(version=0)
        h.bus.failures = {"CreateSession": portal.PortalError("CreateSession: UnknownMethod")}
        with self.assertLogs(capture_portal.LOGGER, level="ERROR") as logs:
            h.hooks.start()
            wait_for(lambda: h.failures, what="the failed CreateSession")
        self.assertIsInstance(h.failures[0], capture_portal.CaptureUnavailable)
        self.assertIn("CreateSession: UnknownMethod", h.failures[0].sentence)
        self.assertIn("CreateSession: UnknownMethod", "\n".join(logs.output))

    # Review fixes (Codex Sol and a cold Opus pass, 01-10-2026)

    def test_a_release_that_fails_ends_the_session_rather_than_leaving_input_captured(self):
        h = self.make()
        h.start()
        h.activate()

        def broken(body):
            raise portal.PortalError("Release failed")

        h.bus.returns["Release"] = broken
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.failures, what="the failure")
        wait_for(lambda: h.bus.closed, what="the connection closed")

    def test_input_sent_away_with_nothing_captured_is_brought_home(self):
        h = self.make()
        h.start()
        h.away = True  # the tray, or a driven machine's move that passed input_held() a moment too early
        wait_for(lambda: h.homes, what="input home")
        self.assertFalse(h.away)

    def test_input_sent_away_from_another_thread_during_a_press_keeps_the_key_that_follows(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.activate()
        h.away = True
        h.ei.feed(("key", h.keyboard, 1, True), ("frame", h.keyboard, 0))
        wait_for(lambda: h.keys, what="the key away")
        self.assertEqual(h.keys, [("esc", True, 1)])
        self.assertEqual(h.releases(), [])

    def test_the_desktop_ending_a_press_the_sender_already_took_away_brings_it_home(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.activate()
        h.away = True
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Deactivated", (SESSION, vardict({"activation_id": 3})))
        wait_for(lambda: h.homes, what="input home")

    def test_a_barrier_request_the_portal_never_answers_ends_the_capture(self):
        saved = capture_portal.REQUEST_SECONDS
        capture_portal.REQUEST_SECONDS = 0.3
        try:
            h = self.make()
            h.bus.answers["SetPointerBarriers"] = None
            h.hooks.start()
            wait_for(lambda: h.failures, what="the failure")
        finally:
            capture_portal.REQUEST_SECONDS = saved

    def test_an_activation_with_no_id_is_still_let_go(self):
        h = self.make(push_to_cross=99)
        h.start()
        h.ei.feed(("start", h.pointer, 1))
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"cursor_position": (1920.0, 500.0), "barrier_id": 1})))
        wait_for(lambda: h.hooks.holding, what="the capture")
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.bus.made("Release"), what="the let-go")
        self.assertNotIn("activation_id", h.bus.made("Release")[0][1])

    def test_a_push_that_meets_two_barriers_at_once_finds_its_wall_from_the_pointer(self):
        h = self.make(models=[return_edge.CornerPush("top_right", "right")], push_to_cross=99)
        h.start()
        h.activate(x=1920.0, y=3.0, barrier=0)
        h.ei.feed(("motion", h.pointer, 4.0, -4.0))
        wait_for(lambda: h.motions, what="the corner push")
        self.assertTrue(h.hooks.holding)

    def test_input_from_an_earlier_activation_never_feeds_a_later_one(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 3), ("motion", h.pointer, 10.0, 0.0), ("motion", h.pointer, 10.0, 0.0),
                  ("stop", h.pointer), ("start", h.pointer, 4))
        time.sleep(0.15)
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 4, "cursor_position": (1920.0, 9.0), "barrier_id": 1})))
        wait_for(lambda: h.hooks.holding, what="the capture")
        time.sleep(0.15)
        self.assertEqual(h.motions, [])

    def test_a_landing_reported_while_the_release_is_in_flight_still_places_the_pointer(self):
        h = self.make()
        h.start()
        h.cross()
        moved = []
        desktop_portal.set_mover(lambda x, y: moved.append((x, y)))

        def late_landing(body):
            desktop_portal.note_position(0, 640)
            return ()

        h.bus.returns["Release"] = late_landing
        h.away = False
        wait_for(lambda: moved, timeout=2, what="the late landing")
        self.assertEqual(moved, [(0, 640)])
        desktop_portal.set_mover(None)

    # Second review (Opus, 01-10-2026)

    def test_a_corner_push_reported_off_the_barrier_line_finds_its_wall(self):
        for corner, point, push in (("bottom_right", (1919.0, 1079.0), (4.0, 4.0)), ("top_right", (1950.0, 3.0), (4.0, -4.0))):
            h = self.make(models=[return_edge.CornerPush(corner, "right")], push_to_cross=99)
            h.start()
            h.activate(x=point[0], y=point[1], barrier=0)
            h.ei.feed(("motion", h.pointer, *push))
            wait_for(lambda: h.motions, what=f"the {corner} push")
            self.assertTrue(h.hooks.holding, corner)
            h.hooks.stop()

    def test_input_away_with_nothing_captured_keeps_being_brought_home(self):
        h = self.make()
        h.hooks.on_home = lambda: h.homes.append(True)  # a sender slow to come home
        h.start()
        h.away = True
        wait_for(lambda: len(h.homes) >= 2, timeout=3, what="a second try")

    def test_a_newer_captures_input_ahead_of_its_signal_is_kept_for_it(self):
        h = self.make()
        h.start()
        h.activate(activation=3)
        h.ei.feed(("motion", h.pointer, -5.0, 0.0))
        wait_for(lambda: h.releases(), what="the let-go")
        h.ei.feed(("stop", h.pointer), ("stop", h.keyboard))
        time.sleep(0.1)
        # The next capture's input arrives before its Activated signal.
        h.ei.feed(("start", h.pointer, 4), ("motion", h.pointer, 10.0, 0.0))
        time.sleep(0.1)
        h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Activated",
                   (SESSION, vardict({"activation_id": 4, "cursor_position": (1920.0, 9.0), "barrier_id": 1})))
        wait_for(lambda: h.motions, what="the newer capture's push")

    def test_a_capture_the_desktop_never_announces_is_let_go(self):
        h = self.make()
        h.start()
        h.ei.feed(("start", h.pointer, 6), ("motion", h.pointer, 10.0, 0.0))
        wait_for(lambda: h.releases(), timeout=capture_portal.EARLY_SECONDS + 1.5, what="the let-go")
        self.assertEqual(h.releases()[0][0], 6)

    def test_repeated_disabled_signals_do_not_spin(self):
        h = self.make()
        h.start()
        for _ in range(5):
            h.bus.emit(portal.OBJECT_PATH, capture_portal.INTERFACE, "Disabled", (SESSION, {}))
        time.sleep(0.4)
        self.assertLessEqual(len(h.bus.made("SetPointerBarriers")), 2)

    def test_version_two_starts_with_the_kept_token_and_keeps_the_new_one(self):
        h = self.make(version=2, tokens=MemoryTokens("old"))
        h.bus.answers["Start"] = (0, {"capabilities": 3, "restore_token": "new"})
        h.start()
        self.assertEqual(h.bus.made("CreateSession"), [])
        options = h.bus.made("Start")[0][2]
        self.assertEqual((options["restore_token"], options["persist_mode"]), (("s", "old"), ("u", 2)))
        self.assertEqual(h.tokens.token, "new")

    def test_a_refusal_under_version_two_forgets_the_token(self):
        h = self.make(version=2, tokens=MemoryTokens("old"))
        h.bus.answers["Start"] = (1, {})
        h.hooks.start()
        wait_for(lambda: h.failures, what="the refusal")
        self.assertEqual(h.tokens.token, "")


if __name__ == "__main__":
    unittest.main()
