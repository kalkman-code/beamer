"""GitHub issue 3: a keyboard on one script driving a machine on another. A PC switched to Russian
sent its C key as "с", which a Mac on English cannot type, so it arrived as text with Ctrl
cleared and Ctrl+C typed "с"; a Mac on Russian did the same to a PC on English. Keys now carry
their place on a US keyboard (`us`), from the scan code here and the key code on the Mac, and a
character the receiving layout cannot type lands on that place when a shortcut needs it or its
letter is from another script than the one typed there."""

import time
import unittest
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import capture_win
from core import protocol
from core import receiver
import sender
from fakes import FakeClipboard, FakeDesktop
from input_injector import KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, plan_key_inputs
from test_key_up_after_switch_back import MONITORS, make_config

VK_C, VK_2 = 0x43, 0x32
SCANS = {VK_C: 0x2E, VK_2: 0x03}
# VkKeyScanW on each layout, and what the key at each US place is and types there.
RUSSIAN = {"с": VK_C, "С": 0x100 | VK_C, "2": VK_2}
ENGLISH = {"c": VK_C, "C": 0x100 | VK_C, "2": VK_2}
RUSSIAN_PLACES = {"c": (VK_C, "с"), "2": (VK_2, "2")}
ENGLISH_PLACES = {"c": (VK_C, "c"), "2": (VK_2, "2")}


def plan(name, layout, places, mods=(), us=None, down=True, held=None):
    return plan_key_inputs(
        name, down, set(mods), {} if held is None else held,
        lambda ch: layout.get(ch, -1), lambda vk: SCANS.get(vk, 0), us, places.get,
    )


class PlaceTests(unittest.TestCase):
    def test_cmd_c_from_a_mac_on_english_is_ctrl_c_on_a_pc_on_russian(self):
        self.assertEqual(plan("c", RUSSIAN, RUSSIAN_PLACES, {"cmd"}, us="c"), [(VK_C, 0x2E, 0)])

    def test_cmd_c_from_a_mac_on_russian_is_ctrl_c_on_a_pc_on_english(self):
        self.assertEqual(plan("с", ENGLISH, ENGLISH_PLACES, {"cmd"}, us="c"), [(VK_C, 0x2E, 0)])

    def test_a_letter_from_the_other_script_types_this_pcs_letter(self):
        self.assertEqual(plan("с", ENGLISH, ENGLISH_PLACES, us="c"), [(VK_C, 0x2E, 0)])
        self.assertEqual(plan("c", RUSSIAN, RUSSIAN_PLACES, us="c"), [(VK_C, 0x2E, 0)])
        self.assertEqual(plan("С", ENGLISH, ENGLISH_PLACES, {"shift"}, us="c"), [(VK_C, 0x2E, 0)])

    def test_a_capital_without_shift_is_this_pcs_capital(self):
        self.assertEqual(plan("С", ENGLISH, ENGLISH_PLACES, us="c"), [(0, ord("C"), KEYEVENTF_UNICODE)])

    def test_a_capital_without_shift_lets_go_of_the_character_it_typed(self):
        self.assertEqual(plan("С", ENGLISH, ENGLISH_PLACES, us="c", down=False), [(0, ord("C"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)])

    def test_a_repeat_after_a_layout_switch_stays_on_the_key_that_is_down(self):
        held = {}
        plan("с", ENGLISH, ENGLISH_PLACES, us="c", held=held)
        self.assertEqual(plan("с", RUSSIAN, {"c": (0x99, "с")}, us="c", held=held), [(VK_C, 0x2E, 0)])
        self.assertEqual(plan("с", RUSSIAN, {"c": (0x99, "с")}, us="c", down=False, held=held), [(VK_C, 0x2E, KEYEVENTF_KEYUP)])

    def test_the_release_lets_go_of_the_key_the_press_went_down_on(self):
        held = {}
        plan("с", ENGLISH, ENGLISH_PLACES, us="c", held=held)
        self.assertEqual(plan("с", ENGLISH, ENGLISH_PLACES, us="c", down=False, held=held), [(VK_C, 0x2E, KEYEVENTF_KEYUP)])
        self.assertEqual(held, {})

    def test_an_accent_from_the_same_script_is_still_typed_as_text(self):
        # French é sits on the US 2 key; an English PC has no é, and 2 is not what was meant.
        self.assertEqual(plan("é", ENGLISH, ENGLISH_PLACES, us="2"), [(0, ord("é"), KEYEVENTF_UNICODE)])

    def test_a_shortcut_on_an_accented_key_lands_on_its_place(self):
        self.assertEqual(plan("é", ENGLISH, ENGLISH_PLACES, {"cmd"}, us="2"), [(VK_2, 0x03, 0)])

    def test_a_key_with_no_place_keeps_the_old_answer(self):
        # A phone's keyboard, or a Mac on an older Beamer, sends no place.
        self.assertEqual(plan("с", ENGLISH, ENGLISH_PLACES, {"cmd"}), [(0, ord("с"), KEYEVENTF_UNICODE)])

    def test_the_receiver_hands_the_place_on(self):
        class Injector:
            def __init__(self):
                self.calls = []

            def inject_key(self, name, down, us=None):
                self.calls.append((name, down, us))

        injector = Injector()
        receiver.handle_message({"type": protocol.MSG_KEYDOWN, "data": {"key": "с", "us": "c"}}, injector=injector)
        receiver.handle_message({"type": protocol.MSG_KEYUP, "data": {"key": "с"}}, injector=injector)
        self.assertEqual(injector.calls, [("с", True, "c"), ("с", False, None)])


class CaptureTests(unittest.TestCase):
    def test_scan_codes_name_the_us_place(self):
        self.assertEqual(capture_win.SCAN_TO_US[0x2E], "c")
        self.assertEqual(capture_win.SCAN_TO_US[0x10], "q")
        self.assertEqual(capture_win.SCAN_TO_US[0x02], "1")
        self.assertEqual(capture_win.SCAN_TO_US[0x29], "`")
        self.assertEqual(capture_win.SCAN_TO_US[0x2B], "\\")
        self.assertEqual(capture_win.SCAN_TO_US[0x35], "/")
        self.assertEqual(len(capture_win.SCAN_TO_US), 47)


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.sender = sender.MacSender(desktop=FakeDesktop(MONITORS, cursor=(0, 500)), clipboard=FakeClipboard(), is_local=lambda host: False)
        self.sender.update_config(make_config())
        self.sender._sock = object()
        self.sender._connected_at = time.monotonic()
        self.sender._last_ack_at = time.monotonic()
        self.sender.set_redirecting(True, arrival_edge="right", offset=0.5)

    def tearDown(self):
        self.sender._sock = None

    def keys(self):
        sent = []
        while not self.sender._outbound.empty():
            message = self.sender._outbound.get_nowait()
            if message.get("type") in (protocol.MSG_KEYDOWN, protocol.MSG_KEYUP):
                sent.append((message["type"], message["data"]))
        return sent

    def test_a_key_carries_its_place_both_ways(self):
        self.sender.on_key("с", True, VK_C, "c")
        self.sender.on_key("с", False, VK_C, "c")
        self.assertEqual(self.keys(), [
            (protocol.MSG_KEYDOWN, {"key": "с", "us": "c"}),
            (protocol.MSG_KEYUP, {"key": "с", "us": "c"}),
        ])

    def test_the_release_sent_as_input_comes_home_carries_it_too(self):
        self.sender.on_key("с", True, VK_C, "c")
        self.sender.set_redirecting(False)
        self.assertEqual(self.keys()[-1], (protocol.MSG_KEYUP, {"key": "с", "us": "c"}))

    def test_a_named_key_has_no_place(self):
        self.sender.on_key("enter", True, 0x0D, None)
        self.assertEqual(self.keys(), [(protocol.MSG_KEYDOWN, {"key": "enter"})])


if __name__ == "__main__":
    unittest.main()
