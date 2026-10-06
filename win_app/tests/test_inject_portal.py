"""Wayland injection and its clipboard on fakes: key steps for a desktop that repeats keys itself
and whose keymap cannot change, the wheel in libei's units, and the RemoteDesktop session against
a scripted portal and libei: the remembered permission, the devices, the pointer kept on the
displays, keys and buttons let go, a refusal said in the app's words, and the clipboard both ways."""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import clipboard_portal
import desktop_portal
import inject_portal
import portal
from inject_x11 import MAP, NO_REPEAT, PRESS, RELEASE, REPEAT
from portal_fakes import FakeBus, FakeDevice, FakeEi, vardict, wait_for

SESSION = "/org/freedesktop/portal/desktop/session/1_42/beamer2"


class FakeKeymap:
    """A US keymap's letters and digits, as xkb_x11.Keymap answers: keycodes are evdev + 8, the
    Shift bit is 1 and Caps Lock 2."""

    KEYS = {38: ("a", "A"), 10: ("1", "!"), 39: ("s", "S")}
    min_keycode, max_keycode = 8, 255

    def keysym(self, keycode, mods, group):
        pair = self.KEYS.get(keycode)
        if pair is None:
            return 0
        return ord(pair[1] if mods & 1 else pair[0])

    def text(self, keysym):
        return chr(keysym) if keysym else None

    def empty(self, keycode):
        return keycode not in self.KEYS

    def core_mods(self, mask):
        return mask


class StepsTest(unittest.TestCase):
    def test_a_key_with_shift_worked_around_it_is_let_go_at_once_and_repeat_and_map_mean_nothing(self):
        steps = [(NO_REPEAT, 38), (PRESS, 50), (PRESS, 38), (RELEASE, 50), (REPEAT, 38), (MAP, 200, 0x41, False)]
        self.assertEqual(inject_portal.wayland_steps(steps), [(42, True), (30, True), (30, False), (42, False)])

    def test_the_wheel_in_120ths_with_the_fraction_carried_and_pixels_as_they_are(self):
        carry = [0.0, 0.0]
        self.assertEqual(inject_portal.scroll_steps(1, 0, "line", carry), ("discrete", 0, -120))
        self.assertEqual(inject_portal.scroll_steps(0.004, 0, "line", carry), ("discrete", 0, 0))
        self.assertEqual(inject_portal.scroll_steps(0.005, -1, "line", carry), ("discrete", -120, -1))
        self.assertEqual(inject_portal.scroll_steps(-3.5, 2, "pixel", carry), ("pixels", 2.0, 3.5))

    def test_a_layout_with_no_key_for_a_character_has_no_spare_to_map(self):
        layout = inject_portal.WaylandLayout(FakeKeymap())
        self.assertEqual(layout.spare(), [])
        state = inject_portal.KeyState()
        self.assertEqual(inject_portal.plan_key("é", True, None, state, layout, 0, 0), [])


class MemoryTokens:
    def __init__(self, token=""):
        self.token = token

    def read(self):
        return self.token

    def write(self, token):
        self.token = token


@unittest.skipIf(sys.platform == "win32", "the Wayland back ends select on pipes, which Windows cannot; they run only on Linux")
class RemoteTest(unittest.TestCase):
    def setUp(self):
        desktop_portal._position = None
        desktop_portal._zones = []
        desktop_portal._regions = []
        desktop_portal._captured = False
        desktop_portal._mover = None
        self.problems = []
        inject_portal.on_problem = self.problems.append
        self.bus = FakeBus(versions={inject_portal.INTERFACE: 2, inject_portal.CLIPBOARD: 1})
        self.bus.answers["CreateSession"] = (0, {"session_handle": SESSION})
        self.bus.answers["Start"] = (0, {"devices": 3, "clipboard_enabled": True, "restore_token": "new"})
        self.bus.returns["ConnectToEIS"] = (99,)
        self.ei = FakeEi()
        self.tokens = MemoryTokens("old")
        self.remote = inject_portal.Remote(connect=lambda: self.bus, ei=lambda fd: self.ei, tokens=self.tokens,
                                           keymap=lambda text: FakeKeymap())
        inject_portal._remote = self.remote
        clipboard_portal.forget_sync()

    def tearDown(self):
        self.remote.stop()
        inject_portal._remote = None
        inject_portal.on_problem = None

    def up(self):
        self.remote.start()
        wait_for(self.remote.ready.is_set, what="the session")
        self.absolute = FakeDevice("absolute", {2, 16, 32}, regions=[(0, 0, 1920, 1080, 1.0)])
        self.pointer = FakeDevice("pointer", {1, 16, 32})
        self.keyboard = FakeDevice("keyboard", {4}, keymap="xkb_keymap {}")
        devices = (self.absolute, self.pointer, self.keyboard)
        self.ei.feed(("seat", 7), *[("device_added", d) for d in devices], *[("device_resumed", d) for d in devices])
        wait_for(lambda: len(self.ei.sends("start")) == 3, what="the devices to emulate")
        wait_for(lambda: self.remote.layout is not None, what="the layout")

    def test_the_session_asks_with_the_kept_permission_and_keeps_the_new_one(self):
        self.up()
        names = [method for _interface, method, _body in self.bus.calls if method not in ("Get",)]
        self.assertEqual(names[:5], ["CreateSession", "SelectDevices", "RequestClipboard", "Start", "ConnectToEIS"])
        options = self.bus.made("SelectDevices")[0][1]
        self.assertEqual((options["restore_token"], options["persist_mode"], options["types"]), (("s", "old"), ("u", 2), ("u", 3)))
        self.assertEqual(self.tokens.token, "new")
        self.assertEqual(desktop_portal.monitors()[0].width, 1920)

    def test_clipboard_version_zero_still_requests_clipboard_before_start(self):
        self.bus.versions[inject_portal.CLIPBOARD] = 0
        self.up()
        self.assertEqual(len(self.bus.made("RequestClipboard")), 1)

    def test_remote_desktop_version_zero_attempts_the_required_calls_without_v2_options(self):
        self.bus.versions[inject_portal.INTERFACE] = 0
        self.up()
        options = self.bus.made("SelectDevices")[0][1]
        self.assertNotIn("persist_mode", options)
        self.assertEqual(len(self.bus.made("ConnectToEIS")), 1)

    def test_the_pointer_lands_moves_and_stays_on_the_displays(self):
        self.up()
        desktop_portal.set_cursor_position(100, 100)
        inject_portal.inject_mouse_move(10, -5)
        inject_portal.inject_mouse_move(5000, 0)
        wait_for(lambda: len(self.ei.sends("absolute")) == 3, what="three moves")
        self.assertEqual([item[2:] for item in self.ei.sends("absolute")], [(100.0, 100.0), (110.0, 95.0), (1919.0, 95.0)])
        self.assertEqual(desktop_portal.cursor_position(), (1919, 95))

    def test_nothing_is_moved_for_real_while_the_capture_holds_the_pointer(self):
        self.up()
        desktop_portal.set_captured(True)
        desktop_portal.set_cursor_position(300, 300)
        inject_portal.inject_mouse_button("left", True)
        wait_for(lambda: self.ei.sends("button"), what="the button")
        self.assertEqual(self.ei.sends("absolute"), [])
        self.assertEqual(desktop_portal.cursor_position(), (300, 300))

    def test_keys_play_by_evdev_code_with_shift_worked_around_and_all_are_let_go(self):
        self.up()
        inject_portal.inject_key("A", True)
        inject_portal.inject_key("A", False)
        inject_portal.inject_key("ctrl", True)
        inject_portal.inject_key("A", True)  # under a chord the peer's own Shift is never touched
        inject_portal.inject_mouse_button("left", True)
        inject_portal.release_all()
        wait_for(lambda: len(self.ei.sends("button")) == 2, what="the release")
        keys = [item[2:] for item in self.ei.sends("key")]
        self.assertEqual(keys, [(42, True), (30, True), (30, False), (42, False), (29, True), (30, True),
                                (29, False), (30, False)])
        self.assertEqual([item[2:] for item in self.ei.sends("button")], [(0x110, True), (0x110, False)])

    def test_text_is_typed_with_the_peers_modifiers_let_go_around_it(self):
        self.up()
        inject_portal.inject_key("ctrl", True)
        inject_portal.inject_text("a!")
        wait_for(lambda: len(self.ei.sends("key")) >= 9, what="the text")
        keys = [item[2:] for item in self.ei.sends("key")]
        self.assertEqual(keys, [(29, True), (29, False), (30, True), (30, False),
                                (42, True), (2, True), (42, False), (2, False), (29, True)])

    def test_the_wheel_goes_out_as_clicks(self):
        self.up()
        inject_portal.inject_scroll(-2, 0, "line")
        wait_for(lambda: self.ei.sends("scroll_discrete"), what="the wheel")
        self.assertEqual(self.ei.sends("scroll_discrete")[0][2:], (0, 240))

    def test_a_refusal_forgets_the_permission_and_says_so_in_the_apps_words(self):
        self.bus.answers["Start"] = (1, {})
        self.remote.start()
        wait_for(lambda: self.problems, what="the refusal")
        self.assertEqual(self.problems, [inject_portal.REFUSED])
        self.assertEqual(self.tokens.token, "")
        self.assertIsNone(clipboard_portal.change_stamp())
        self.assertEqual(clipboard_portal.get_contents(), (None, None))

    def test_a_desktop_without_remote_control_over_libei_is_said_plainly(self):
        self.bus.versions[inject_portal.INTERFACE] = 1
        self.remote.start()
        wait_for(lambda: self.problems, what="the problem")
        self.assertEqual(self.problems, [inject_portal.NO_PORTAL])

    def test_beamers_clipboard_is_written_to_the_app_that_pastes_it(self):
        self.up()
        before = clipboard_portal.change_stamp()
        self.assertTrue(clipboard_portal.set_contents("hello\r\nthere", None))
        self.assertEqual(clipboard_portal.change_stamp(), before + 1)
        offered = self.bus.made("SetSelection")[0][1]["mime_types"][1]
        self.assertIn("text/plain;charset=utf-8", offered)
        read, write = os.pipe()
        self.bus.returns["SelectionWrite"] = (write,)
        self.bus.emit(portal.OBJECT_PATH, inject_portal.CLIPBOARD, "SelectionOwnerChanged",
                      (SESSION, vardict({"mime_types": offered, "session_is_owner": True})))
        self.bus.emit(portal.OBJECT_PATH, inject_portal.CLIPBOARD, "SelectionTransfer", (SESSION, "text/plain;charset=utf-8", 4))
        with os.fdopen(read, "rb") as handle:
            self.assertEqual(handle.read(), b"hello\nthere")
        wait_for(lambda: self.bus.made("SelectionWriteDone"), what="the transfer to finish")
        self.assertEqual(self.bus.made("SelectionWriteDone")[0][1:], (4, True))
        self.assertEqual(clipboard_portal.change_stamp(), before + 1)  # its own echo is no change
        self.assertEqual(clipboard_portal.changed_contents(), (None, None))

    def test_another_apps_copy_is_read_once_and_moves_the_stamp(self):
        self.up()
        before = clipboard_portal.change_stamp()
        self.bus.emit(portal.OBJECT_PATH, inject_portal.CLIPBOARD, "SelectionOwnerChanged",
                      (SESSION, vardict({"mime_types": ["UTF8_STRING", "text/plain;charset=utf-8"], "session_is_owner": False})))
        wait_for(lambda: clipboard_portal.change_stamp() == before + 1, what="the copy")

        def selection(body):
            read, write = os.pipe()
            threading.Thread(target=lambda: (os.write(write, b"from another app"), os.close(write))).start()
            return (read,)

        self.bus.returns["SelectionRead"] = selection
        self.assertEqual(clipboard_portal.changed_contents(), ("from another app", None))
        self.assertEqual(self.bus.made("SelectionRead")[0][1], "text/plain;charset=utf-8")
        self.assertEqual(clipboard_portal.changed_contents(), (None, None))
        self.assertEqual(clipboard_portal.get_contents(), ("from another app", None))

    def test_an_app_that_never_hands_the_clipboard_over_costs_one_read(self):
        self.up()
        self.bus.emit(portal.OBJECT_PATH, inject_portal.CLIPBOARD, "SelectionOwnerChanged",
                      (SESSION, vardict({"mime_types": ["text/plain"], "session_is_owner": False})))
        wait_for(lambda: self.remote.offered, what="the copy")
        read, write = os.pipe()
        self.bus.returns["SelectionRead"] = (read,)
        saved = clipboard_portal.TIMEOUT_SECONDS
        clipboard_portal.TIMEOUT_SECONDS = 0.2
        try:
            self.assertEqual(clipboard_portal._read_fd(read, clipboard_portal.TIMEOUT_SECONDS), None)
        finally:
            clipboard_portal.TIMEOUT_SECONDS = saved
            os.close(write)

    def test_the_session_closing_lets_go_and_says_so(self):  # closed from the desktop
        self.up()
        inject_portal.inject_key("shift", True)
        wait_for(lambda: self.ei.sends("key"), what="the key")
        self.bus.emit(SESSION, portal.SESSION, "Closed", ())
        wait_for(lambda: self.problems, what="the problem")
        self.assertEqual(self.problems[0], inject_portal.CLOSED)
        self.assertEqual(self.ei.sends("key")[-1][2:], (42, False))
        self.assertTrue(self.ei.closed and self.bus.closed)


    # Review fixes (Codex Sol and a cold Opus pass, 01-10-2026)

    def test_a_paste_the_portal_fails_costs_that_paste_and_not_the_session(self):
        self.up()
        clipboard_portal.set_contents("x", None)

        def broken(body):
            raise portal.PortalError("stale serial")

        self.bus.returns["SelectionWrite"] = broken
        self.bus.emit(portal.OBJECT_PATH, inject_portal.CLIPBOARD, "SelectionTransfer", (SESSION, "text/plain", 9))
        wait_for(lambda: self.bus.made("SelectionWriteDone"), what="the failed transfer answered")
        self.assertEqual(self.bus.made("SelectionWriteDone")[0][1:], (9, False))
        self.assertTrue(self.remote.ready.is_set())
        self.assertEqual(self.problems, [])

    def test_a_session_that_ended_opens_again_without_asking_but_a_refusal_stands(self):
        self.up()
        saved = inject_portal.RETRY_SECONDS
        inject_portal.RETRY_SECONDS = 0.0
        try:
            self.ei.feed(("disconnect",))
            wait_for(lambda: self.problems, what="the problem")
            wait_for(lambda: not self.remote.ready.is_set(), what="the session down")
            self.bus.closed = False
            self.ei = FakeEi()
            inject_portal.inject_key("ctrl", True)
            wait_for(self.remote.ready.is_set, what="the session again")
        finally:
            inject_portal.RETRY_SECONDS = saved

    def test_nothing_queues_once_the_desktop_has_refused(self):
        self.bus.answers["Start"] = (1, {})
        self.remote.start()
        wait_for(lambda: self.problems, what="the refusal")
        for _ in range(50):
            inject_portal.inject_mouse_move(1, 1)
        self.assertEqual(self.remote._commands.qsize(), 0)

    def test_input_sent_before_the_devices_resume_is_played_once_they_do(self):
        self.remote.start()
        wait_for(self.remote.ready.is_set, what="the session")
        inject_portal.inject_key("ctrl", True)
        time.sleep(0.2)
        self.keyboard = FakeDevice("keyboard", {4}, keymap="xkb_keymap {}")
        self.absolute = FakeDevice("absolute", {2, 16, 32}, regions=[(0, 0, 1920, 1080, 1.0)])
        self.ei.feed(("device_added", self.keyboard), ("device_added", self.absolute),
                     ("device_resumed", self.keyboard), ("device_resumed", self.absolute))
        wait_for(lambda: self.ei.sends("key"), what="the held key")
        self.assertEqual(self.ei.sends("key")[0][1:], ("keyboard", 29, True))

    def test_buttons_and_the_wheel_go_to_the_devices_that_have_them(self):
        self.remote.start()
        wait_for(self.remote.ready.is_set, what="the session")
        motion = FakeDevice("motion", {1})
        buttons = FakeDevice("buttons", {32})
        wheel = FakeDevice("wheel", {16})
        keyboard = FakeDevice("keyboard", {4}, keymap="xkb_keymap {}")
        devices = (motion, buttons, wheel, keyboard)
        self.ei.feed(*[("device_added", d) for d in devices], *[("device_resumed", d) for d in devices])
        wait_for(lambda: len(self.ei.sends("start")) == 4, what="the devices")
        inject_portal.inject_mouse_button("left", True)
        inject_portal.inject_scroll(1, 0, "line")
        wait_for(lambda: self.ei.sends("scroll_discrete"), what="the wheel")
        self.assertEqual(self.ei.sends("button")[0][1], "buttons")
        self.assertEqual(self.ei.sends("scroll_discrete")[0][1], "wheel")

    def test_an_fd_answered_after_its_caller_gave_up_is_closed(self):
        self.up()
        read, write = os.pipe()
        gate = threading.Event()

        def slow(session):
            gate.wait(2)
            return read

        self.assertIsNone(self.remote.ask(slow, timeout=0.1, discard=os.close))
        gate.set()
        wait_for(lambda: _closed(read), what="the late fd closed")
        os.close(write)

    def test_a_paste_that_never_reads_is_given_up_on(self):
        self.up()
        saved = inject_portal.WRITE_SECONDS
        inject_portal.WRITE_SECONDS = 0.3
        try:
            clipboard_portal.set_contents("y" * 200000, None)
            read, write = os.pipe()
            self.bus.returns["SelectionWrite"] = (write,)
            self.bus.emit(portal.OBJECT_PATH, inject_portal.CLIPBOARD, "SelectionTransfer", (SESSION, "text/plain", 5))
            wait_for(lambda: self.bus.made("SelectionWriteDone"), what="the transfer given up")
            self.assertEqual(self.bus.made("SelectionWriteDone")[0][1:], (5, False))
            os.close(read)
        finally:
            inject_portal.WRITE_SECONDS = saved

    def test_input_after_a_stop_does_not_open_the_session_again(self):
        self.up()
        self.remote.stop()
        inject_portal.inject_key("ctrl", True)
        time.sleep(0.2)
        self.assertFalse(self.remote._thread is not None and self.remote._thread.is_alive())


    # Second review (Opus, 01-10-2026)

    def test_a_button_held_on_a_device_the_desktop_removes_is_forgotten_not_played(self):
        self.up()
        inject_portal.inject_mouse_button("left", True)
        wait_for(lambda: self.ei.sends("button"), what="the press")
        holder = next(d for d in (self.pointer, self.absolute) if d.name == self.ei.sends("button")[0][1])
        self.ei.feed(("device_removed", holder))
        inject_portal.inject_mouse_button("left", False)
        inject_portal.inject_key("ctrl", True)
        wait_for(lambda: self.ei.sends("key"), what="the session carrying on")
        self.assertEqual(len(self.ei.sends("button")), 1)

    def test_a_release_is_admitted_however_full_the_queue(self):
        self.up()
        inject_portal.inject_key("ctrl", True)
        inject_portal.inject_mouse_button("left", True)
        wait_for(lambda: self.ei.sends("key") and self.ei.sends("button"), what="the presses")
        saved = inject_portal.MAX_PENDING
        inject_portal.MAX_PENDING = 0
        try:
            inject_portal.inject_mouse_move(5, 5)  # dropped: the queue is full
            inject_portal.inject_key("ctrl", False)
            inject_portal.inject_mouse_button("left", False)
            wait_for(lambda: len(self.ei.sends("key")) == 2 and len(self.ei.sends("button")) == 2, what="the releases")
        finally:
            inject_portal.MAX_PENDING = saved
        self.assertEqual([item[2:] for item in self.ei.sends("key")], [(29, True), (29, False)])

    def test_a_missing_library_is_said_once_and_never_retried(self):
        def missing():
            raise RuntimeError("libei is not installed; Beamer needs it to capture and play input on Wayland")

        factory = lambda fd: self.ei  # noqa: E731
        factory.preflight = missing
        self.remote._ei_factory = factory
        saved = inject_portal.RETRY_SECONDS
        inject_portal.RETRY_SECONDS = 0.0
        try:
            self.remote.start()
            wait_for(lambda: self.problems, what="the problem")
            for _ in range(5):
                inject_portal.inject_key("ctrl", True)
                time.sleep(0.05)
            self.assertEqual(len(self.problems), 1)
            self.assertIn("libei", self.problems[0])
            self.assertEqual(self.bus.made("CreateSession"), [])
        finally:
            inject_portal.RETRY_SECONDS = saved

    def test_a_session_the_user_stopped_from_the_desktop_stays_stopped(self):
        self.up()
        saved = inject_portal.RETRY_SECONDS
        inject_portal.RETRY_SECONDS = 0.0
        try:
            self.bus.emit(SESSION, portal.SESSION, "Closed", ())
            wait_for(lambda: self.problems, what="the problem")
            wait_for(lambda: not self.remote.ready.is_set(), what="the session down")
            self.bus.closed = False  # a new connection could be made
            inject_portal.inject_key("ctrl", True)
            time.sleep(0.2)
            self.assertEqual(len(self.bus.made("CreateSession")), 1)
        finally:
            inject_portal.RETRY_SECONDS = saved

    def test_a_session_with_no_remembered_permission_is_not_reopened_to_ask_again(self):
        self.bus.answers["Start"] = (0, {"devices": 3, "clipboard_enabled": True})  # the box unticked: no token
        self.up()
        saved = inject_portal.RETRY_SECONDS
        inject_portal.RETRY_SECONDS = 0.0
        try:
            self.ei.feed(("disconnect",))
            wait_for(lambda: self.problems, what="the problem")
            wait_for(lambda: not self.remote.ready.is_set(), what="the session down")
            self.bus.closed = False  # a new connection could be made
            inject_portal.inject_key("ctrl", True)
            time.sleep(0.2)
            self.assertEqual(len(self.bus.made("CreateSession")), 1)
        finally:
            inject_portal.RETRY_SECONDS = saved


def _closed(fd):
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


class DesktopTest(unittest.TestCase):
    def test_a_point_off_every_display_is_brought_to_the_nearest(self):
        displays = [desktop_portal.Rect(0, 0, 1920, 1080), desktop_portal.Rect(1920, 200, 1280, 1024)]
        self.assertEqual(desktop_portal.clamp_to(displays, 1920, 100), (1919, 100))
        self.assertEqual(desktop_portal.clamp_to(displays, 3300, 700.4), (3199, 700))
        self.assertEqual(desktop_portal.clamp_to(displays, 2500, 1500), (2500, 1223))
        self.assertEqual(desktop_portal.clamp_to([], 5, 6), (5, 6))


if __name__ == "__main__":
    unittest.main()
