import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import desktop_x11
from core.return_edge import Rect

WIN_APP = Path(__file__).resolve().parent.parent


class FakeXError(Exception):
    pass


class FakeClosed(Exception):
    pass


class FakeProperty:
    def __init__(self, value):
        self.value = value


class FakeWindow:
    def __init__(self, wid, state=(), wm_class=("kate", "Kate"), error=None):
        self.wid, self.state, self.wm_class, self.error = wid, state, wm_class, error

    def get_full_property(self, atom, kind):
        if self.error:
            raise self.error
        return FakeProperty(list(self.state)) if self.state is not None else None

    def get_wm_class(self):
        if self.error:
            raise self.error
        return self.wm_class


class FakeRoot:
    def __init__(self, display):
        self.display = display
        self.pointer = SimpleNamespace(root_x=0, root_y=0, mask=0)
        self.geometry = SimpleNamespace(width=1600, height=900)
        self.monitor_list = []
        self.monitor_error = None
        self.warps = []
        self.active = None
        self.calls = []

    def xrandr_get_monitors(self, is_active=True):
        self.calls.append(("xrandr_get_monitors", is_active))
        if self.monitor_error:
            raise self.monitor_error
        return SimpleNamespace(monitors=self.monitor_list)

    def get_geometry(self):
        return self.geometry

    def query_pointer(self):
        if self.display.dead:
            raise FakeClosed()
        return self.pointer

    def warp_pointer(self, x, y):
        self.warps.append((x, y))

    def get_full_property(self, atom, kind):
        assert atom == self.display.intern_atom("_NET_ACTIVE_WINDOW")
        return None if self.active is None else FakeProperty([self.active])


class FakeDisplay:
    ATOMS = {"_NET_ACTIVE_WINDOW": 11, "_NET_WM_STATE": 12, "_NET_WM_STATE_FULLSCREEN": 13, "_NET_WM_STATE_ABOVE": 14}

    def __init__(self, randr=(1, 5)):
        self.randr = randr
        self.root = FakeRoot(self)
        self.windows = {}
        self.flushes = 0
        self.closed = False
        self.dead = False

    def has_extension(self, name):
        return name == "RANDR" and self.randr is not None

    def xrandr_query_version(self):
        return SimpleNamespace(major_version=self.randr[0], minor_version=self.randr[1])

    def intern_atom(self, name):
        return self.ATOMS[name]

    def create_resource_object(self, kind, wid):
        assert kind == "window"
        return self.windows[wid]

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


def connection(display):
    return SimpleNamespace(display=display, root=display.root, x_error=FakeXError, closed=(FakeClosed, OSError))


class DesktopX11Test(unittest.TestCase):
    def setUp(self):
        self.display = FakeDisplay()
        self.root = self.display.root
        self.connects = mock.Mock(side_effect=lambda: connection(self.display))
        patch = mock.patch.object(desktop_x11, "_connect", self.connects)
        patch.start()
        self.addCleanup(patch.stop)
        desktop_x11._reset()
        self.addCleanup(desktop_x11._reset)

    def two_monitors(self):
        self.root.monitor_list = [
            SimpleNamespace(x=-1280, y=100, width_in_pixels=1280, height_in_pixels=1024),
            SimpleNamespace(x=0, y=0, width_in_pixels=1920, height_in_pixels=1080),
        ]

    def test_monitors_from_randr_one_five(self):
        self.two_monitors()
        self.assertEqual(desktop_x11.monitors(), [Rect(-1280, 100, 1280, 1024), Rect(0, 0, 1920, 1080)])
        self.assertEqual(self.root.calls, [("xrandr_get_monitors", True)])

    def test_monitors_falls_back_to_the_root_without_randr(self):
        self.display.randr = None
        self.two_monitors()
        self.assertEqual(desktop_x11.monitors(), [Rect(0, 0, 1600, 900)])
        self.assertEqual(self.root.calls, [])

    def test_monitors_falls_back_to_the_root_on_randr_one_four(self):
        self.display.randr = (1, 4)
        self.two_monitors()
        self.assertEqual(desktop_x11.monitors(), [Rect(0, 0, 1600, 900)])
        self.assertEqual(self.root.calls, [])

    def test_monitors_accepts_a_later_major_version(self):
        self.display.randr = (2, 0)
        self.two_monitors()
        self.assertEqual(len(desktop_x11.monitors()), 2)

    def test_monitors_falls_back_to_the_root_when_the_request_fails(self):
        self.root.monitor_error = FakeXError()
        self.assertEqual(desktop_x11.monitors(), [Rect(0, 0, 1600, 900)])

    def test_monitors_falls_back_to_the_root_when_randr_lists_none(self):
        self.assertEqual(desktop_x11.monitors(), [Rect(0, 0, 1600, 900)])

    def test_cursor_position(self):
        self.root.pointer = SimpleNamespace(root_x=-40, root_y=612, mask=0)
        self.assertEqual(desktop_x11.cursor_position(), (-40, 612))

    def test_set_cursor_position_warps_with_ints_and_flushes_once(self):
        desktop_x11.set_cursor_position(321.0, 123.9)
        self.assertEqual(self.root.warps, [(321, 123)])
        for value in self.root.warps[0]:
            self.assertIs(type(value), int)
        self.assertEqual(self.display.flushes, 1)

    def test_button_down_reads_the_pointer_mask(self):
        cases = {"left": 1 << 8, "middle": 1 << 9, "right": 1 << 10}
        for name, bit in cases.items():
            for other in cases:
                with self.subTest(name=name, mask=other):
                    self.root.pointer = SimpleNamespace(root_x=0, root_y=0, mask=cases[other] | 1)
                    self.assertEqual(desktop_x11.button_down(name), other == name)
            self.root.pointer = SimpleNamespace(root_x=0, root_y=0, mask=0)
            self.assertFalse(desktop_x11.button_down(name))

    def test_button_down_is_false_for_back_forward_and_unknown(self):
        self.root.pointer = SimpleNamespace(root_x=0, root_y=0, mask=0x1F00)
        for name in ("back", "forward", "wheel", ""):
            with self.subTest(name=name):
                self.assertFalse(desktop_x11.button_down(name))

    def fullscreen(self, **kwargs):
        window = FakeWindow(77, state=kwargs.pop("state", (13,)), **kwargs)
        self.display.windows[77] = window
        self.root.active = 77
        return window

    def test_full_screen_app_names_the_window_class(self):
        self.fullscreen()
        self.assertEqual(desktop_x11.full_screen_app(), "Kate")

    def test_full_screen_app_is_none_when_not_full_screen(self):
        self.fullscreen(state=(14,))
        self.assertIsNone(desktop_x11.full_screen_app())

    def test_full_screen_app_is_none_without_a_state_property(self):
        self.fullscreen(state=None)
        self.assertIsNone(desktop_x11.full_screen_app())

    def test_full_screen_app_is_none_without_an_active_window(self):
        self.assertIsNone(desktop_x11.full_screen_app())
        self.root.active = 0
        self.assertIsNone(desktop_x11.full_screen_app())

    def test_full_screen_app_without_a_wm_class_is_an_app(self):
        self.fullscreen(wm_class=None)
        self.assertEqual(desktop_x11.full_screen_app(), "An app")
        self.fullscreen(wm_class=("kate", ""))
        self.assertEqual(desktop_x11.full_screen_app(), "An app")

    def test_full_screen_app_is_none_when_the_window_vanishes(self):
        self.fullscreen(error=FakeXError())
        self.assertIsNone(desktop_x11.full_screen_app())

    def test_one_connection_is_shared_across_calls(self):
        desktop_x11.cursor_position()
        desktop_x11.button_down("left")
        desktop_x11.set_cursor_position(1, 2)
        self.assertEqual(self.connects.call_count, 1)

    def test_a_dropped_connection_reconnects_on_the_next_call(self):
        first = self.display
        desktop_x11.cursor_position()
        first.dead = True
        with self.assertRaises(FakeClosed):
            desktop_x11.cursor_position()
        self.assertTrue(first.closed)
        self.display = FakeDisplay()
        self.display.root.pointer = SimpleNamespace(root_x=5, root_y=6, mask=0)
        self.assertEqual(desktop_x11.cursor_position(), (5, 6))
        self.assertEqual(self.connects.call_count, 2)

    def test_an_oserror_also_drops_the_connection(self):
        desktop_x11.cursor_position()
        with mock.patch.object(self.root, "query_pointer", side_effect=BrokenPipeError()):
            with self.assertRaises(OSError):
                desktop_x11.cursor_position()
        desktop_x11.cursor_position()
        self.assertEqual(self.connects.call_count, 2)

    def test_an_x_error_keeps_the_connection(self):
        with mock.patch.object(self.root, "query_pointer", side_effect=FakeXError()):
            with self.assertRaises(FakeXError):
                desktop_x11.cursor_position()
        desktop_x11.cursor_position()
        self.assertEqual(self.connects.call_count, 1)

    def test_a_failed_connect_is_retried(self):
        self.connects.side_effect = [OSError("no display"), connection(self.display)]
        with self.assertRaises(OSError):
            desktop_x11.cursor_position()
        desktop_x11.cursor_position()
        self.assertEqual(self.connects.call_count, 2)


class ImportTest(unittest.TestCase):
    def test_imports_without_loading_xlib(self):
        code = (
            "import sys; sys.path.append('..'); import desktop_x11; "
            "sys.exit(1 if any(m == 'Xlib' or m.startswith('Xlib.') for m in sys.modules) else 0)"
        )
        done = subprocess.run([sys.executable, "-c", code], cwd=WIN_APP, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
