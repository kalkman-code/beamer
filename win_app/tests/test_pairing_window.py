"""Overview's machines and the pairing sheet in the real window, built offscreen as
the README's screenshots build it, which starts no receiver, hooks or announcer."""

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import pairing, peerlist, protocol
from core.tests import responder_harness as harness

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    import app_config
    import kvm_bridge_win
    import theme
except ImportError:  # PySide6 is only in the Windows venv
    kvm_bridge_win = None

TOKEN_A = protocol.id_text(bytes(range(32)))
TOKEN_B = protocol.id_text(bytes(range(1, 33)))
ID_A = b"\x0a" * 15 + b"\x01"
ID_B = b"\x0b" * 15 + b"\x02"


def peer(identity, name, token, host, platform="macos", paired_at=1_780_000_000, in_use=True):
    entry = pairing.peer_entry(identity, name, platform, 24820, token, host, paired_at)
    entry["in_use"] = in_use
    return entry


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class MachinesWindowTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        self.folder = Path(tempfile.mkdtemp())
        self.path = self.folder / "settings.json"
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.server.stop()
            self.window.deleteLater()

    def open(self, *entries, **top):
        settings = app_config.load_settings(self.path)
        settings["peers"] = list(entries)
        settings["port"] = harness.free_port()
        settings.update(top)
        app_config.write_settings(self.path, settings)
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        self.stub(self.window)
        self.window._refresh_window()
        return self.window

    def stub(self, window):
        """No real listener, hook or link: what would start them is recorded instead."""
        self.calls = []
        window._start_receiver = lambda config: self.calls.append("receiver")
        window.hooks = SimpleNamespace(start=lambda: self.calls.append("hooks"), stop=lambda: self.calls.append("hooks off"))
        window.sender.start = lambda config: self.calls.append("sender")
        window.sender.stop = lambda: self.calls.append("sender off")

    def settings(self):
        return json.loads(self.path.read_text())

    def test_two_machines_with_one_name_show_the_end_of_their_ids(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "studio", TOKEN_B, "192.0.2.11", "windows"))
        labels = [row.name.text() for row in window.machines.rows.values()]
        self.assertEqual(labels, [f"Studio ({protocol.id_text(ID_A)[-4:]})", f"studio ({protocol.id_text(ID_B)[-4:]})"])
        self.assertEqual(window.machines.empty.isHidden(), True)

    def test_a_row_names_the_platform_and_address_and_a_state(self):
        window = self.open(peer(ID_A, "Studio Mac", TOKEN_A, "192.0.2.10"))
        row = next(iter(window.machines.rows.values()))
        self.assertEqual(row.where.text(), "Mac · 192.0.2.10 · port 24820")
        self.assertEqual(row.word.text(), "Waiting")
        self.assertIn("Studio Mac", row.detail.text())

    def test_the_direction_switches_start_under_a_closed_disclosure(self):
        window = self.open(peer(ID_A, "Studio Mac", TOKEN_A, "192.0.2.10"))
        row = next(iter(window.machines.rows.values()))
        self.assertFalse(row.directions.isChecked())
        self.assertTrue(row.direction_group.isHidden())
        row.directions.click()
        self.assertFalse(row.direction_group.isHidden())

    def test_the_send_button_names_the_only_machine_in_use(self):
        window = self.open(peer(ID_A, "Studio Mac", TOKEN_A, "192.0.2.10"),
                           peer(ID_B, "Inactive", TOKEN_B, "192.0.2.11", in_use=False))
        window.sender.shortcut_target = lambda: None
        self.assertEqual(window._redirect_text(), "Send input to Studio Mac")

    def test_the_paired_announcement_omits_machines_not_in_use(self):
        window = self.open(peer(ID_A, "Studio Mac", TOKEN_A, "192.0.2.10"),
                           peer(ID_B, "Inactive", TOKEN_B, "192.0.2.11", in_use=False))
        sent = []
        window._send_to = lambda ident, message: sent.append((ident, message)) or True
        window._send_paired()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ID_A)
        self.assertEqual(sent[0][1]["data"]["ids"], [])

    def test_any_token_from_1_4_x_asks_to_pair_again(self):
        # "A" * 43 has pairing's shape: 1.4.x kept no record of whether a token was paired or typed.
        for token in ("typed by hand", "A" * 43):
            with self.subTest(token=token):
                for name in ("config.json", "settings.json"):
                    (self.folder / name).unlink(missing_ok=True)
                (self.folder / "config.json").write_text(json.dumps({
                    "host": "192.0.2.20", "port": harness.free_port(), "auth_token": token, "paired_with": "MacBook Pro",
                    "mac_host": "192.0.2.10", "mac_return_edge": "left"}))
                self.window = kvm_bridge_win.WindowsApplication(self.path)
                row = next(iter(self.window.machines.rows.values()))
                self.assertEqual(row.word.text(), "Pair again")
                self.window.server.stop()
                self.window.deleteLater()
                self.window = None

    def test_with_nothing_paired_the_list_says_so(self):
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        self.assertFalse(self.window.machines.empty.isHidden())
        self.assertEqual(self.window.machines.empty.text(), "No machines paired yet.")
        self.assertFalse(self.window._paired)
        self.assertFalse(self.window.same_switch.isEnabled())

    def test_with_nothing_paired_appearance_and_speed_still_save(self):
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        self.stub(self.window)
        self.window._appearance_chosen("dark")
        self.window._speed_changed("pointer_speed", 150)
        self.window._reverse_scroll_changed(True)
        self.window._persist()
        saved = self.settings()
        self.assertEqual((saved["appearance"], saved["pointer_speed"], saved["reverse_scroll"]), ("dark", 1.5, True))
        self.assertEqual(saved["peers"], [])
        again = kvm_bridge_win.WindowsApplication(self.path)
        self.stub(again)
        self.assertEqual((again._config.appearance, again._config.pointer_speed), ("dark", 1.5))
        again.deleteLater()

    def test_a_switch_writes_that_machines_entry_only(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        window._set_peer_send(TOKEN_B, False)
        window._set_peer_allow(TOKEN_A, False)
        by_name = {entry["name"]: entry for entry in self.settings()["peers"]}
        self.assertEqual((by_name["Laptop"]["send"], by_name["Laptop"]["allow_drive"]), (False, True))
        self.assertEqual((by_name["Studio"]["send"], by_name["Studio"]["allow_drive"]), (True, False))
        window._refresh_window()
        rows = list(window.machines.rows.values())
        self.assertEqual([(row.send.isChecked(), row.allow.isChecked()) for row in rows], [(True, False), (False, True)])

    def test_the_tray_send_items_omit_machines_not_in_use(self):
        entries = [
            peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"),
            peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"),
            peer(b"\x0c" * 15 + b"\x03", "Spare", protocol.id_text(bytes(range(2, 34))), "192.0.2.12"),
        ]
        window = self.open(*entries)
        window._edit_peer(protocol.id_text(bytes(range(2, 34))), in_use=False)
        window._after_peers_edit()
        window._refresh_tray_machines()
        self.assertEqual(list(window.machine_actions), [protocol.id_text(ID_A), protocol.id_text(ID_B)])

    def test_the_trays_ticks_change_every_machine_in_one_write(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        window._set_every_peer("allow_drive", False)
        self.assertEqual([entry["allow_drive"] for entry in self.settings()["peers"]], [False, False])
        window._refresh_window()
        self.assertFalse(window.drive_action.isChecked())
        self.assertTrue(window.send_action.isChecked())

    def test_removing_a_machine_takes_its_entry_and_zones_and_keeps_the_rest(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        settings = self.settings()
        settings["zones"] = [{"peer": protocol.id_text(ID_A), "kind": "edge"}, {"peer": protocol.id_text(ID_B), "kind": "edge", "off": True}]
        app_config.write_settings(self.path, settings)
        window._remove_peer(TOKEN_A)
        after = self.settings()
        self.assertEqual([entry["name"] for entry in after["peers"]], ["Laptop"])
        self.assertEqual([zone["peer"] for zone in after["zones"]], [protocol.id_text(ID_B)])
        self.assertEqual(list(window.machines.rows), [TOKEN_B])
        self.assertEqual(window._config.auth_token, TOKEN_B)

    def test_removing_the_last_machine_leaves_the_window_unpaired(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"))
        window._remove_peer(TOKEN_A)
        self.assertEqual(self.settings()["peers"], [])
        self.assertFalse(window._paired)
        self.assertFalse(window.same_switch.isEnabled())
        self.assertFalse(window.machines.empty.isHidden())
        window._refresh_window()

    def test_the_first_pairing_makes_the_window_paired(self):
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        self.stub(self.window)
        entry = peer(ID_A, "Studio", TOKEN_A, "192.0.2.10")
        self.window._store_peer(entry, None)
        self.window._on_paired(entry)
        self.assertTrue(self.window._paired)
        self.assertEqual(list(self.window.machines.rows), [TOKEN_A])
        self.assertEqual(self.window.sheet.host_note.text(), "Paired with Studio.")

    def test_pairing_a_machine_starts_sending_when_nothing_could_send_before(self):
        (self.folder / "config.json").write_text(json.dumps({
            "host": "192.0.2.20", "port": harness.free_port(), "auth_token": "typed by hand", "paired_with": "MacBook Pro",
            "mac_host": "192.0.2.10", "mac_return_edge": "left"}))
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        self.stub(self.window)
        self.assertNotIn("sender", self.calls)
        entry = peer(ID_A, "Studio", TOKEN_A, "192.0.2.10")
        self.window._store_peer(entry, self.window.book.peers()[0])
        self.window._on_paired(entry)
        self.assertIn("sender", self.calls)
        self.assertIn("hooks", self.calls)

    def test_removing_the_only_machine_that_could_be_sent_to_stops_sending(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        window._set_peer_send(TOKEN_B, False)
        self.calls.clear()
        window._remove_peer(TOKEN_A)
        self.assertIn("hooks off", self.calls)
        self.assertIn("sender off", self.calls)

    def input_on(self, window, peer):
        homes = []
        window.sender._owner = SimpleNamespace(on=peer, away=peer is not None)
        window.sender.set_redirecting = lambda value, came_home=True: homes.append(value)
        return homes

    def test_turning_send_off_for_the_machine_input_is_on_brings_it_home_first(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        homes = self.input_on(window, ID_A)
        window._set_peer_send(TOKEN_A, False)
        self.assertEqual(homes, [False])

    def test_turning_send_off_for_every_machine_brings_input_home_first(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        self.input_on(window, ID_A)
        order = []
        window.sender.set_redirecting = lambda value, came_home=True: order.append("home")
        refresh = window.sender.refresh
        window.sender.refresh = lambda: (order.append("links follow"), refresh())
        window._set_every_peer("send", False)
        self.assertEqual(order[0], "home")
        self.assertEqual([entry["send"] for entry in self.settings()["peers"]], [False, False])

    def test_input_comes_home_before_send_off_is_saved(self):
        # The links read `send` from the settings on their own each second: one that read it in
        # between would end itself with input still on its machine.
        from unittest import mock
        for turn_off in (lambda window: window._set_peer_send(TOKEN_A, False),
                         lambda window: window._set_every_peer("send", False)):
            window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"))
            self.input_on(window, ID_A)
            order = []
            window.sender.set_redirecting = lambda value, came_home=True: order.append("home")
            write = kvm_bridge_win.app_config.write_settings
            with mock.patch.object(kvm_bridge_win.app_config, "write_settings", side_effect=lambda *a, **k: (order.append("saved"), write(*a, **k))[1]):
                turn_off(window)
            self.assertEqual(order[:2], ["home", "saved"])

    def test_turning_send_on_for_every_machine_or_drive_off_leaves_input_where_it_is(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"))
        homes = self.input_on(window, ID_A)
        window._set_every_peer("send", True)
        window._set_every_peer("allow_drive", False)
        self.assertEqual(homes, [])

    def test_turning_send_off_for_another_machine_leaves_input_where_it_is(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        homes = self.input_on(window, ID_A)
        window._set_peer_send(TOKEN_B, False)
        self.assertEqual(homes, [])

    def test_the_connection_pages_port_is_this_pcs_and_leaves_every_machines_port_alone(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"))
        window.port_entry.setText("24830")
        window.save()
        after = self.settings()
        self.assertEqual(after["port"], 24830)
        self.assertEqual([entry["port"] for entry in after["peers"]], [24820])
        self.assertEqual(window._announced_port(), 24830)

    def test_the_port_a_pairing_proves_survives_the_last_machine_going(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), port=24830)
        window._remove_peer(TOKEN_A)
        self.assertFalse(window._paired)
        self.assertEqual(window._announced_port(), 24830)
        self.assertEqual(window._identity()["port"], 24830)

    def test_hide_addresses_holds_with_no_machine_paired(self):
        window = self.open(hide_addresses=True)
        self.assertTrue(window.hide_switch.isChecked())
        self.assertEqual(window._shown("Reach it at 192.0.2.5"), "Reach it at •••")
        window.hide_switch.setChecked(False)
        self.assertFalse(self.settings()["hide_addresses"])
        window.hide_switch.setChecked(True)
        self.assertTrue(self.settings()["hide_addresses"])

    def test_a_label_that_is_an_address_is_hidden_with_the_rest(self):
        window = self.open(peer(ID_A, "", TOKEN_A, "192.0.2.10"), hide_addresses=True)
        row = next(iter(window.machines.rows.values()))
        self.assertNotIn("192.0.2.10", row.name.text())
        self.assertNotIn("192.0.2.10", row.detail.text())

    def test_a_pairing_replaces_the_machine_it_was_meant_to_and_keeps_it_first(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        replaced = window.book.peers()[0]
        fresh = peer(b"\x0c" * 16, "Studio", protocol.id_text(bytes(range(2, 34))), "192.0.2.10")
        window._store_peer(fresh, replaced)
        self.assertEqual([entry["name"] for entry in self.settings()["peers"]], ["Studio", "Laptop"])
        self.assertEqual(self.settings()["peers"][0]["id"], protocol.id_text(b"\x0c" * 16))

    def test_a_pairing_does_not_replace_a_machine_that_linked_since_it_was_read(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        replaced = window.book.peers()[0]
        settings = self.settings()
        settings["peers"][0]["linked"] = True
        app_config.write_settings(self.path, settings)
        fresh = peer(b"\x0c" * 16, "Studio", protocol.id_text(bytes(range(2, 34))), "192.0.2.10")
        with self.assertRaises(Exception):
            window._store_peer(fresh, replaced)
        self.assertEqual([entry["token"] for entry in self.settings()["peers"]], [TOKEN_A, TOKEN_B])
        self.assertTrue(self.settings()["peers"][0]["linked"])

    def test_a_pairing_does_not_replace_a_machine_that_was_removed_since_it_was_read(self):
        window = self.open(peer(ID_A, "Studio", TOKEN_A, "192.0.2.10"), peer(ID_B, "Laptop", TOKEN_B, "192.0.2.11"))
        replaced = window.book.peers()[0]
        window._remove_peer(TOKEN_A)
        fresh = peer(b"\x0c" * 16, "Studio", protocol.id_text(bytes(range(2, 34))), "192.0.2.10")
        with self.assertRaises(Exception):
            window._store_peer(fresh, replaced)
        self.assertEqual([entry["token"] for entry in self.settings()["peers"]], [TOKEN_B])

    def test_pairing_the_mac_at_the_1_4_entrys_address_again_keeps_its_side_and_zones(self):
        # Every 1.4.x upgrader pairs again (no token migrates since 1.5.0), on purpose and at the
        # screen: where that Mac sits is not theirs to set a second time.
        migrated = {**peer(ID_A, "Studio Mac", TOKEN_A, "192.0.2.10"), "id": "", "from_1_4": True, "linked": False,
                    "paired_at": 0, "side": "top", "side_set_at": 1_790_000_000}
        window = self.open(migrated, zones=[{"peer": "", "kind": "edge"}, {"peer": "", "kind": "corner",
                                                                           "corner": "top_left", "edge": "top", "off": True}])
        fresh = peer(b"\x0c" * 16, "Studio Mac", protocol.id_text(bytes(range(2, 34))), "192.0.2.10")
        window._store_peer(fresh, window.book.peers()[0])
        after = self.settings()
        new_id = protocol.id_text(b"\x0c" * 16)
        self.assertEqual([zone["peer"] for zone in after["zones"]], [new_id, new_id])
        self.assertEqual((after["peers"][0]["side"], after["peers"][0]["side_set_at"]), ("top", 1_790_000_000))

    def test_pairing_another_platform_at_that_address_keeps_nothing_of_it(self):
        migrated = {**peer(ID_A, "Studio Mac", TOKEN_A, "192.0.2.10"), "id": "", "from_1_4": True, "linked": False,
                    "paired_at": 0, "side": "top", "side_set_at": 1_790_000_000}
        window = self.open(migrated, zones=[{"peer": "", "kind": "edge"}])
        fresh = peer(b"\x0c" * 16, "Studio PC", protocol.id_text(bytes(range(2, 34))), "192.0.2.10", platform="windows")
        window._store_peer(fresh, window.book.peers()[0])
        after = self.settings()
        # Only its own whole edge, which every machine paired starts with.
        self.assertEqual(after["zones"], [{"peer": protocol.id_text(b"\x0c" * 16), "kind": "edge"}])
        self.assertEqual(after["peers"][0]["side"], "")


@unittest.skipIf(kvm_bridge_win is None, "needs PySide6")
class PairingSheetWindowTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        theme.init_fonts()
        folder = Path(tempfile.mkdtemp())
        self.path = folder / "settings.json"
        self.window = kvm_bridge_win.WindowsApplication(self.path)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            spare = probe.getsockname()[1]
        # Its own TCP port, so a real Beamer or another suite on this machine never meets it.
        self.service = self.window.pairing = pairing.PairingService(
            "Test PC", b"\x09" * 16, "windows", lambda: 24820, self.window.book.peers, self.window._store_peer,
            on_paired=self.window.bridge.paired.emit, tcp_port=spare,
        )
        self.window._toggle_pairing_sheet()

    def tearDown(self):
        self.service.cancel_pairing()
        self.window.server.stop()
        self.window.deleteLater()

    def show_code(self):
        self.window._show_code()
        return self.service.code

    def test_showing_a_code_fills_the_sheet(self):
        code = self.show_code()
        self.assertEqual(self.window.sheet.code_label.text(), f"{code[:3]} {code[3:]}")
        self.assertTrue(self.window.sheet.code_showing)
        self.assertTrue(self.window.sheet.count_label.text().startswith("Expires in "))

    def test_pairing_code_text_is_centred_in_its_stretched_label(self):
        self.show_code()
        alignment = self.window.sheet.code_label.alignment()
        self.assertTrue(alignment & Qt.AlignmentFlag.AlignHCenter)

    def test_cancelling_the_code_puts_the_button_back_and_says_nothing(self):
        self.show_code()
        self.window._cancel_code()
        self.assertFalse(self.window.sheet.code_showing)
        self.assertEqual(self.window.sheet.host_note.text(), "")

    def test_closing_the_sheet_cancels_a_code_that_is_up(self):
        self.show_code()
        self.window._toggle_pairing_sheet()
        self.assertIsNone(self.service.code)

    def test_a_wrong_code_ends_the_code_and_says_so(self):
        self.show_code()
        host = self.service.host
        client = pairing.PairingClient(host.pair_id, "000000" if host.code != "000000" else "000001", "Test machine",
                                       b"\x01" * 16, version=pairing.PAIRING_V3, platform="macos", port=24820)
        host.handle(client.start(), "192.0.2.50")
        host.handle(client.abort(), "192.0.2.50")
        self.window._refresh_pairing()
        self.assertIsNone(self.service.code)
        self.assertEqual(self.window.sheet.host_note.text(), peerlist.host_outcome_text("refused", None))

    def test_a_machine_on_the_old_exchange_is_listed_as_older_and_chosen_at_once(self):
        old = {"name": "Old Mac", "address": "192.0.2.60", "port": 24820, "reply_port": 24821, "pair_id": "x",
               "pairing": 2, "platform": None}
        self.window.sheet.set_machines([old])
        button = self.window.sheet.group.buttons()[0]
        self.assertIn("Older Beamer", button.text())
        button.click()
        self.assertEqual(self.window.sheet.pair_note.text(),
                         peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_VERSION), "Old Mac"))

    STUDIO = {"name": "Studio", "address": "192.168.50.22", "port": 24820, "reply_port": 24821,
              "pair_id": "first", "pairing": 3, "platform": "windows"}

    def test_one_machine_heard_on_two_interfaces_shows_each_row_s_address(self):
        twice = [self.STUDIO, dict(self.STUDIO, address="169.254.3.7", pair_id="second")]
        self.window.sheet.set_machines(twice, self.window._shown)
        self.assertEqual([button.text() for button in self.window.sheet.group.buttons()], [
            "Studio  ·  Windows  ·  192.168.50.22  ·  showing a code",
            "Studio  ·  Windows  ·  169.254.3.7  ·  showing a code",
        ])

    def test_a_lone_machine_s_row_is_as_it_was(self):
        self.window.sheet.set_machines([self.STUDIO], self.window._shown)
        self.assertEqual(self.window.sheet.group.buttons()[0].text(), "Studio  ·  Windows  ·  showing a code")

    def test_the_addresses_follow_hide_addresses(self):
        twice = [self.STUDIO, dict(self.STUDIO, address="169.254.3.7", pair_id="second")]
        self.window.sheet.set_machines(twice, self.window._shown)
        self.window._config.hide_addresses = True
        self.window.sheet.set_machines(twice, self.window._shown)
        texts = [button.text() for button in self.window.sheet.group.buttons()]
        self.assertEqual(len(texts), 2)
        self.assertFalse([text for text in texts if "192.168.50.22" in text or "169.254.3.7" in text], texts)

    def test_only_a_machine_showing_a_code_is_listed(self):
        quiet = {"name": "Quiet", "address": "192.0.2.61", "port": 24820, "reply_port": 24821, "pair_id": None,
                 "pairing": 3, "platform": "macos"}
        self.window.sheet.set_machines([quiet])
        self.assertEqual(self.window.sheet.group.buttons(), [])
        self.assertFalse(self.window.sheet.none_heard.isHidden())

    def test_pair_waits_for_a_chosen_machine_or_an_address_and_six_digits(self):
        sheet = self.window.sheet
        self.assertFalse(sheet.pair_button.isEnabled())
        sheet.address.setText("192.0.2.70")
        sheet._typed_address("192.0.2.70")
        sheet._typed_code("482 913")
        self.assertEqual(sheet.code.text(), "482913")
        self.assertTrue(sheet.pair_button.isEnabled())

    def test_the_machine_pair_sends_is_the_one_last_heard_not_the_one_first_listed(self):
        first = {"name": "Mac", "address": "192.0.2.60", "port": 24820, "reply_port": 1, "pair_id": "old", "pairing": 3, "platform": "macos"}
        self.window.sheet.set_machines([first])
        self.window.sheet.group.buttons()[0].click()
        self.window.sheet.set_machines([dict(first, pair_id="new")])
        self.assertEqual(self.window.sheet.chosen_machine()["pair_id"], "new")

    def test_pair_stays_off_while_a_pairing_is_under_way(self):
        sheet = self.window.sheet
        sheet.address.setText("192.0.2.70")
        sheet._typed_address("192.0.2.70")
        sheet.code.setText("482913")
        sheet._typed_code("482913")
        self.assertTrue(sheet.pair_button.isEnabled())
        sheet.busy(True)
        sheet._typed_code("482913")
        self.assertFalse(sheet.pair_button.isEnabled())
        sheet.busy(False)
        self.assertTrue(sheet.pair_button.isEnabled())

    def test_a_pair_error_hides_an_address_when_addresses_are_hidden(self):
        self.window._config = kvm_bridge_win.default_config()
        self.window._config.hide_addresses = True
        self.window._pairing_target = "192.0.2.70"
        self.window._on_pair_finished(None, pairing.PairingError("no_answer"))
        self.assertNotIn("192.0.2.70", self.window.sheet.pair_note.text())

    def test_a_failed_pairing_says_why_in_the_peers_name(self):
        self.window._pairing_target = "Studio"
        self.window._on_pair_finished(None, pairing.PairingError(pairing.ERROR_REFUSED))
        self.assertEqual(self.window.sheet.pair_note.text(),
                         peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_REFUSED), "Studio"))

    def test_the_code_shows_a_qr_unless_addresses_are_hidden(self):
        self.window._code_address = "192.0.2.5"
        self.window._pairing_address = lambda: "192.0.2.5"
        self.show_code()
        self.assertFalse(self.window.sheet.qr.isHidden())
        self.assertIn("192.0.2.5", self.window.sheet.address_note.text())
        self.window._config = kvm_bridge_win.default_config()
        self.window._config.hide_addresses = True
        self.window._refresh_pairing()
        self.assertTrue(self.window.sheet.qr.isHidden())
        self.assertNotIn("192.0.2.5", self.window.sheet.address_note.text())
        self.assertIn("Hide addresses", self.window.sheet.qr_caption.text())


if __name__ == "__main__":
    unittest.main()
