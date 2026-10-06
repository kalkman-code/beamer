"""Phone presentation and targeting, without constructing a window or capturing input."""
import unittest
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kvm_bridge_app
import machines
import pages
from machines_panel import MachinesPanel
from core import protocol
from windows_input import WindowsInput
from bridge import KVMController
from wake import WakingController
from settings_store import editable_default_config
from bridge_fakes import FakeQuartz, FakeClipboard, quiet_logger
from fake_link import FakeLink
from mac_app.tests.test_phone_settings import peer


class PhoneUiTests(unittest.TestCase):
    def setUp(self):
        self.phone, self.desktop = peer(1, 0), peer(2)
        self.peers = [self.phone, self.desktop]
        self.book = mock.Mock()
        self.book.peers.return_value = self.peers

    def test_phone_does_not_affect_same_capability_or_receive_settings(self):
        inbound = WindowsInput.__new__(WindowsInput)
        inbound._controller = SimpleNamespace(book=self.book)
        inbound.server = mock.Mock()
        inbound.server.links.return_value = [protocol.read_id(self.phone["id"])]
        inbound.server.caps_of.return_value = []
        inbound.settings_callback = mock.Mock()
        self.assertIsNone(inbound.peer_settings)
        inbound._on_settings(protocol.read_id(self.phone["id"]), {"on": True})
        inbound.settings_callback.assert_not_called()

    def test_phone_receives_no_settings_even_if_it_advertises_them(self):
        inbound = WindowsInput.__new__(WindowsInput)
        inbound._controller = SimpleNamespace(book=self.book, _peers_up={})
        inbound.server = mock.Mock()
        inbound.server.links.return_value = [protocol.read_id(self.phone["id"])]
        inbound.server.caps_of.return_value = ["settings", "design_sync"]
        self.assertFalse(inbound.send_settings({"on": True}))
        inbound.server.send.assert_not_called()

    def test_crossing_diagram_and_match_caps_exclude_phone(self):
        window = SimpleNamespace(settings_store=SimpleNamespace(current=lambda: {"peers": self.peers, "zones": []}),
                                 edge_select=SimpleNamespace(value="right"), part_boxes={},
                                 corner_select=SimpleNamespace(value="top_right"), chosen_peer=self.desktop["id"],
                                 _shown=lambda text: text,
                                 controller=SimpleNamespace(book=self.book, _peers_up={}), windows_input=mock.Mock())
        window.windows_input.server.links.return_value = [protocol.read_id(self.phone["id"])]
        window.windows_input.server.caps_of.return_value = ["settings", "design_sync"]
        found = kvm_bridge_app.ControlWindow._diagram_machines(window, ["edge"])
        self.assertEqual([m["key"] for m in found], [self.desktop["id"]])
        self.assertEqual(kvm_bridge_app.ControlWindow._design_peer_caps(window), {})

    def test_bulk_send_toggle_excludes_phone(self):
        tray = SimpleNamespace(controller=SimpleNamespace(book=self.book), _switch=mock.Mock())
        kvm_bridge_app.TrayApp.toggle_send_to_windows(tray, None)
        tray._switch.assert_called_once_with([self.desktop["token"]], send=False)

    def test_phone_controls_show_in_use_and_remove_without_directions(self):
        row = machines.row(self.phone, "Phone", False, "none", "", False, False, False, False)
        panel = SimpleNamespace(removing=None, wide=True, directions_open={row.token})
        import widgets
        in_use = widgets.Switch("In use")
        disclosure = widgets.label("Directions")
        remove = widgets.label("Remove")
        result = MachinesPanel._controls(panel, row, in_use, disclosure,
                                        widgets.Switch("This Mac drives it"), widgets.Switch("It drives this Mac"), remove)
        self.assertEqual(len(result.arrangedSubviews()), 1)
        views = list(result.arrangedSubviews()[0].arrangedSubviews())
        self.assertIn(in_use.view, views)
        self.assertIn(remove, views)
        self.assertNotIn(disclosure, views)

    def test_phone_messages_cannot_set_same_or_match(self):
        window = SimpleNamespace(controller=SimpleNamespace(book=self.book), _design_states={})
        kvm_bridge_app.ControlWindow.apply_same(window, {"on": True}, self.phone["id"])
        kvm_bridge_app.ControlWindow._receive_design(window, {"on": True}, self.phone["id"])
        self.assertEqual(window._design_states, {})

    def test_phone_is_never_dialled_even_with_a_stale_send_switch(self):
        self.phone["send"] = True
        self.book.zones.return_value = []
        controller = KVMController(editable_default_config(), quartz=FakeQuartz,
                                   clipboard=FakeClipboard(), logger=quiet_logger(), book=self.book,
                                   link_factory=FakeLink)
        self.addCleanup(controller.stop)
        self.assertNotIn(self.phone["token"], controller.links)
        self.assertIn(self.desktop["token"], controller.links)

    def test_phone_never_supplies_a_wake_address(self):
        self.phone.update(hw="aa:bb:cc:dd:ee:ff", host="192.0.2.20")
        controller = SimpleNamespace(book=self.book, _memory=None)
        self.assertEqual(WakingController._wake_address(controller, self.phone["id"]), ("", ""))

    def test_explicit_phone_wake_is_refused_before_starting_a_worker(self):
        controller = SimpleNamespace(book=self.book, _memory=None, _waking=False,
                                     _wake_lock=threading.Lock(), _awake=lambda peer: False,
                                     _wake_worker=mock.Mock())
        with mock.patch("wake.threading.Thread") as worker:
            self.assertFalse(WakingController.wake(controller, self.phone["id"]))
        worker.assert_not_called()

    def test_phone_is_named_and_has_no_menu_target(self):
        for platform, name in (("ios", "iPhone"), ("android", "Android")):
            row = machines.row({**self.phone, "platform": platform}, "Phone", False, "none", "", False, True, True, False)
            self.assertEqual((row.label, row.platform), ("Phone", name))
            self.assertEqual(row.state.word, "Driving this Mac")
        self.assertEqual(pages.send_items([{**self.phone, "send": True}], None, False), [])
