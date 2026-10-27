"""Mac key injection with native frameworks blocked before the module loads."""

import importlib.util
from collections import namedtuple
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load_injector():
    directory = Path(__file__).resolve().parents[2] / "mac_app"
    spec = importlib.util.spec_from_file_location("mac_repeat_injector", directory / "input_injector_mac.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"Quartz": None, "AppKit": None, "Foundation": None}), \
            patch.object(sys, "path", [str(directory)] + sys.path):
        spec.loader.exec_module(module)
    return module


class QuartzRecorder:
    kCGKeyboardEventAutorepeat = 8
    kCGEventSourceUserData = 42
    kCGHIDEventTap = 0
    kCGEventFlagMaskShift = 1 << 17
    kCGEventFlagMaskControl = 1 << 18
    kCGEventFlagMaskAlternate = 1 << 19
    kCGEventFlagMaskCommand = 1 << 20
    kCGEventFlagMaskAlphaShift = 1 << 16
    kCGEventFlagMaskSecondaryFn = 1 << 23
    kCGEventFlagMaskNumericPad = 1 << 21

    def __init__(self):
        self.events = []

    def CGEventCreateKeyboardEvent(self, source, code, down):
        return {"code": code, "down": down, 8: 0}

    def CGEventSetIntegerValueField(self, event, field, value):
        event[field] = value

    def CGEventSetFlags(self, event, flags):
        event["flags"] = flags

    def CGEventKeyboardSetUnicodeString(self, event, length, text):
        event["text"] = text

    def CGEventPost(self, tap, event):
        self.events.append(event)


class MacKeyRepeatTests(unittest.TestCase):
    def setUp(self):
        self.injector = load_injector()
        layout = self.injector.keyboard_layout
        self.enterContext(patch.multiple(layout, _chars=dict(layout._chars), _codes=dict(layout._codes)))
        self.quartz = QuartzRecorder()
        self.injector.Quartz = self.quartz

    def feed(self, name, states):
        for down in states:
            self.injector.inject_key(name, down)

    def repeats(self):
        return [event[8] for event in self.quartz.events]

    def deliver(self, messages):
        from core import receiver
        responder = SimpleNamespace(_injector_module=lambda: self.injector)
        link = SimpleNamespace(held={}, name="peer")
        for message in messages:
            receiver.LinkResponder._inject_locked(responder, link, message)
        return responder, link

    def test_held_letters_and_named_keys_mark_only_subsequent_downs_as_repeats(self):
        for name in ("a", "n", "backspace", "left", "f5", "enter", "é"):
            with self.subTest(name=name):
                self.quartz.events.clear()
                self.feed(name, (True, True, True, False, True, False))
                self.assertEqual(self.repeats(), [0, 1, 1, 0, 0, 0])
                self.assertEqual([e["down"] for e in self.quartz.events],
                                 [True, True, True, False, True, False])

    def test_each_key_has_its_own_hold(self):
        for name, down in (("a", True), ("n", True), ("a", True),
                           ("n", True), ("a", False), ("n", True), ("n", False)):
            self.injector.inject_key(name, down)
        self.assertEqual(self.repeats(), [0, 0, 1, 1, 0, 1, 0])

    def test_held_modifier_flags_survive_letter_repeat_and_modifiers_do_not_repeat(self):
        self.feed("ctrl", (True, True))
        self.feed("c", (True, True, False))
        self.feed("ctrl", (False,))
        self.assertEqual(self.repeats(), [0, 0, 0, 1, 0, 0])
        self.assertEqual([e["flags"] for e in self.quartz.events], [1 << 18] * 5 + [0])

    def test_layout_change_during_hold_keeps_the_original_keycode(self):
        self.injector.inject_key("z", True)
        self.injector.keyboard_layout.install({6: "y", 16: "z"})
        self.feed("z", (True, False, True, False))
        self.assertEqual([e["code"] for e in self.quartz.events], [6, 6, 6, 16, 16])
        self.assertEqual(self.repeats(), [0, 1, 0, 0, 0])

    def test_release_all_ends_holds_and_the_next_press_is_fresh(self):
        self.feed("shift", (True,))
        for name in ("a", "left", "é"):
            self.feed(name, (True, True))
        self.injector.release_all()
        released = {e["code"] for e in self.quartz.events if not e["down"]}
        self.assertEqual(released, {56, 0, 123})
        self.assertTrue(all(e[8] == 0 for e in self.quartz.events if not e["down"]))
        self.quartz.events.clear()
        for name in ("a", "left", "é"):
            self.feed(name, (True,))
        self.assertEqual(self.repeats(), [0, 0, 0])

    def test_windows_hook_repeats_survive_sender_wire_and_mac_receiver(self):
        from core.tests import test_windows_accent_releases as windows
        case = windows.WindowsReleaseTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        for down in (True, True, False):
            case.feed(0x45, down, 0x12)
        _, link = self.deliver(case.messages())
        self.assertEqual(self.repeats(), [0, 1, 0])
        self.assertEqual(link.held, {})

    def test_x11_capture_repeats_survive_sender_wire_and_mac_receiver(self):
        from core.tests import test_linux_accents as accents
        import capture_x11
        sender = accents.AccentTests.link_sender()
        hooks = capture_x11.Hooks(sender.on_key, lambda *args: False, lambda *args: None)
        hooks._connection = SimpleNamespace(keymap=accents.AccentKeymap())
        for down, repeat in ((True, False), (True, True), (False, False)):
            hooks._key(26, down, 0, 0, False, repeat=repeat)
        _, link = self.deliver(accents.AccentTests.messages(sender))
        self.assertEqual(self.repeats(), [0, 1, 0])
        self.assertEqual(link.held, {})

    def test_wayland_capture_forwards_repeated_libei_downs_without_a_wire_change(self):
        from core.tests import test_linux_accents as accents
        import capture_portal
        sender = accents.AccentTests.link_sender()
        hooks = capture_portal.Hooks(sender.on_key, lambda *args: False, lambda *args: None,
                                     tokens=SimpleNamespace())
        hooks._keymap = accents.AccentKeymap()
        hooks._keymap.core_mods = lambda mods: 0
        device = object()
        for down in (True, True, False):
            hooks._key_event(18, down, device)
        _, link = self.deliver(accents.AccentTests.messages(sender))
        self.assertEqual(self.repeats(), [0, 1, 0])
        self.assertEqual(link.held, {})

    def test_mac_capture_retains_repeated_downs_for_mac_receiver(self):
        from core import protocol
        from core.tests.app_methods import load_methods
        namespace = {"KeyResult": namedtuple("KeyResult", "is_trigger is_down is_repeat messages"),
                     "SPECIAL_KEY_NAMES": {}, "MODIFIER_KEY_CODES": set(),
                     "PRINTABLE_KEY_FALLBACKS": {0: "a"}, "protocol": protocol}
        Translator = load_methods("mac_app/bridge.py", "QuartzEventTranslator", ["key_result"], namespace)
        translator = Translator()
        translator.quartz = SimpleNamespace(kCGKeyboardEventKeycode=9, kCGKeyboardEventAutorepeat=8,
                                           kCGEventFlagsChanged=12, kCGEventKeyDown=10, kCGEventKeyUp=11,
                                           CGEventGetIntegerValueField=lambda event, field: event[field])
        translator.printable_down = {}
        translator._unicode_character = lambda event, code: "a"
        translator._special_key_messages = lambda *args: []
        messages = []
        for kind, repeat in ((10, 0), (10, 1), (11, 0)):
            result = translator.key_result(kind, {9: 0, 8: repeat}, 99, {})
            for message in result.messages:
                message["data"]["seq"] = len(messages) + 1
                messages.append(protocol.read_input(protocol.parse_message(protocol.dumps_message(message))))
        _, link = self.deliver(messages)
        self.assertEqual(self.repeats(), [0, 1, 0])
        self.assertEqual(link.held, {})

    def test_receiver_release_after_repeat_ends_hold_before_next_owner(self):
        from core import receiver
        responder, link = self.deliver([{"type": "keydown", "key": "a", "us": "a"}] * 2)
        receiver.LinkResponder._release_locked(responder, link)
        self.injector.inject_key("a", True)
        self.assertEqual(self.repeats(), [0, 1, 0, 0])
        self.assertEqual(link.held, {})


if __name__ == "__main__":
    unittest.main()
