"""Sender-only phone decisions without creating a window or installing input hooks."""

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

sys.path.append(str(Path(__file__).resolve().parents[2]))
sys.path.append(str(Path(__file__).resolve().parents[1]))

from core import peerlist, protocol, settings_sync
from core.tests.app_methods import load_methods
import pages_win
import peers_view
import sender

PHONE = protocol.id_text(bytes(range(16)))
DESKTOP = protocol.id_text(bytes(range(16, 32)))


def entries():
    return [dict(id=PHONE, token=protocol.id_text(bytes(range(32))), name="My phone", platform="ios", port=0,
                 send=True, allow_drive=True, in_use=True),
            dict(id=DESKTOP, token=protocol.id_text(bytes(range(32, 64))), name="Desktop", platform="windows", port=24820,
                 send=True, allow_drive=True, in_use=True)]


def application(*methods, **extra):
    namespace = dict(Optional=Optional, Config=SimpleNamespace, peerlist=peerlist,
                     protocol=protocol, settings_sync=settings_sync, pages_win=pages_win,
                     Qt=SimpleNamespace(ItemDataRole=SimpleNamespace(UserRole=1)), **extra)
    namespace["TRIGGER_KEYS"] = ()
    return load_methods("win_app/kvm_bridge_win.py", "WindowsApplication", methods, namespace)()


class Control:
    def __init__(self, text=""):
        self.value = text
        self.checked = False
        self.visible = True
        self.enabled = True

    def text(self): return self.value
    def setText(self, text): self.value = text
    def isChecked(self): return self.checked
    def setChecked(self, value): self.checked = value
    def blockSignals(self, value): pass
    def setVisible(self, value): self.visible = value
    def setEnabled(self, value): self.enabled = value
    def setToolTip(self, value): self.tooltip = value
    def setAccessibleName(self, value): self.accessible = value
    def isVisibleTo(self, parent): return self.visible


class PhoneUiTests(unittest.TestCase):
    def test_phone_is_never_woken_even_with_stale_hardware(self):
        attempts = []
        threads = SimpleNamespace(Thread=lambda **fields: SimpleNamespace(start=lambda: attempts.append(fields)))
        link = load_methods("win_app/sender.py", "LinkSender", ["wake"],
                            dict(Optional=Optional, threading=threads))()
        peer = protocol.read_id(PHONE)
        link._peers = {peer: dict(entries()[0], hw="aa:bb:cc:dd:ee:ff")}
        link.links = SimpleNamespace(up=lambda peer: False, refused=lambda peer: False)
        link._wake_lock, link._waking = nullcontext(), set()
        link._wake_worker = lambda *args: None
        self.assertFalse(link.wake(peer))
        self.assertEqual(attempts, [])

    def test_phone_never_learns_hardware_even_with_a_stale_host(self):
        looked_up = []
        link = load_methods("win_app/sender.py", "LinkSender", ["_learn_hardware"], {})()
        peer = protocol.read_id(PHONE)
        link._peers = {peer: dict(entries()[0], host="192.0.2.1", hw="")}
        link._mac_lookup = lambda host: looked_up.append(host) or ""
        link._learn_hardware(peer)
        self.assertEqual(looked_up, [])

    def test_phone_is_never_dialled_even_with_a_saved_host(self):
        made = []
        class Link:
            def __init__(self, token, *args, **kwargs): made.append(token)
        links = sender.LinkSet(SimpleNamespace(_hardware=None, _book=None, _identity=None),
                               is_local=lambda host: False, link_class=Link)
        peers = entries()
        peers[0]["host"] = "192.0.2.1"
        peers[1]["host"] = "192.0.2.2"
        links.sync(peers)
        self.assertEqual(made, [peers[1]["token"]])

    def test_crossing_choice_and_settings_exclude_phone_even_with_stale_zone(self):
        app = application("_crossing_machines", "_crossing_settings")
        app._peer_entries = entries()
        app._zone_entries = [dict(peer=PHONE), dict(peer=DESKTOP)]
        app._config = SimpleNamespace(machine_id="own")
        app._shown = lambda text: text
        self.assertEqual(app._crossing_machines(), [(DESKTOP, "Desktop")])
        self.assertEqual(app._crossing_settings()["peers"], [entries()[1]])
        self.assertEqual(app._crossing_settings()["zones"], [dict(peer=DESKTOP)])

    def test_same_and_match_capabilities_ignore_phone_advertisements(self):
        app = application("_peer_caps", "_design_peer_caps")
        phone, desktop = protocol.read_id(PHONE), protocol.read_id(DESKTOP)
        app._peer_entries = entries()
        app.sender = SimpleNamespace(peers_up=lambda: [phone, desktop],
                                     links=SimpleNamespace(caps=lambda peer: {"settings", "design_sync"}))
        app.server = SimpleNamespace(links=lambda: [phone, desktop], caps_of=lambda peer: {"settings", "design_sync"})
        self.assertEqual(set(app._peer_caps()), {desktop})
        self.assertEqual(set(app._design_peer_caps()), {DESKTOP})

    def test_phone_settings_never_apply_even_when_advertised(self):
        app = application("_on_settings")
        app._peer_entries = entries()
        app.book = SimpleNamespace(peers=lambda: app._peer_entries)
        class Config:
            @property
            def same_on_both(self):
                raise AssertionError("phone settings must be ignored before reading local settings")
        app._config = Config()
        app._design_states = {}
        app._on_settings(PHONE, {"design_sync": {}})
        self.assertEqual(app._design_states, {})

    def test_match_choices_ignore_phone(self):
        class Model:
            def __init__(self, parent): self.items = []
            def appendRow(self, item): self.items.append(item)

        class Item(Control):
            def setData(self, value, role): self.data = value

        app = application("_refresh_design_follow", "_lock_followed_design", QStandardItemModel=Model, QStandardItem=Item)
        app._peer_entries = entries()
        app._config = SimpleNamespace(design_follow_peer="", same_on_both=False)
        app._design_states = {PHONE: {}, DESKTOP: {}}
        app._design_peer_caps = lambda: {PHONE: {"design_sync"}, DESKTOP: {"design_sync"}}
        app._shown = lambda text: text
        choice = app.design_follow_choice = Control()
        choice.setModel = lambda model: setattr(choice, "model", model)
        choice.findData = lambda value, role: 0
        choice.setCurrentIndex = lambda value: None
        app.design_follow_note = Control()
        app._refresh_design_follow()
        self.assertEqual([item.data for item in choice.model.items], ["", DESKTOP])

    def test_bulk_send_does_not_enable_phone_but_receive_can_be_toggled(self):
        settings = {"peers": entries()}
        settings["peers"][0]["send"] = False
        fake_config = SimpleNamespace(SETTINGS_LOCK=nullcontext(), load_settings=lambda path: settings,
                                      write_settings=lambda path, value: None)
        app = application("_set_every_peer", app_config=fake_config)
        app.config_path = "synthetic"
        app.sender = SimpleNamespace(owner=None)
        app._after_peers_edit = lambda: None
        app._set_every_peer("send", True)
        self.assertFalse(settings["peers"][0]["send"])
        app._set_every_peer("allow_drive", False)
        self.assertFalse(settings["peers"][0]["allow_drive"])

    def test_phone_only_tray_has_no_send_direction(self):
        app = application("_refresh_tray_directions")
        app._peer_entries = entries()[:1]
        app._shown = lambda text: text
        app.drive_action, app.send_action = Control(), Control()
        app._refresh_tray_directions()
        self.assertTrue(app.drive_action.enabled)
        self.assertFalse(app.send_action.enabled)
        self.assertFalse(app.send_action.checked)

    def test_tray_send_input_targets_never_offer_a_phone(self):
        class Action(Control):
            def __init__(self, text, parent):
                super().__init__(text)
                self.triggered = SimpleNamespace(connect=lambda callback: None)
        app = application("_refresh_tray_machines", QAction=Action)
        app._peer_entries = entries()
        other = dict(entries()[1], id=protocol.id_text(bytes(range(32, 48))),
                     token="another-desktop", name="Another desktop")
        app._peer_entries.append(other)
        app._shown = lambda text: text
        app.machine_actions = {}
        app.tray_menu = SimpleNamespace(insertAction=lambda before, action: None)
        app.pause_action = object()
        app.redirect_action = Control()
        app.sender = SimpleNamespace(owner=None, redirecting=False)
        app._refresh_tray_machines()
        self.assertEqual(set(app.machine_actions), {DESKTOP, other["id"]})
        self.assertTrue(all("phone" not in action.text() for action in app.machine_actions.values()))

    def test_same_announcements_ignore_phone_even_with_advertised_capabilities_and_side(self):
        from core import return_edge
        app = application("_announce", return_edge=return_edge)
        phone = dict(entries()[0], send=False, side="left", side_set_at=10)
        app.book = SimpleNamespace(peers=lambda: [phone])
        app._config = SimpleNamespace(machine_id=DESKTOP)
        app._peer_caps = lambda: {protocol.read_id(PHONE): {"settings", "design_sync"}}
        self.assertEqual(app._announce(protocol.read_id(PHONE)), [])

    def test_phone_row_has_name_platform_remove_and_in_use_without_directions(self):
        row = load_methods("win_app/machines_win.py", "PeerRow", ["refresh", "_ask"],
                           dict(KEEP_NOTE="kept"))()
        for name in ("name", "word", "where", "detail", "confirm_text", "in_use", "send", "allow",
                     "remove_button", "directions", "direction_group", "confirm"):
            setattr(row, name, Control())
        row.led = SimpleNamespace(set_tone=lambda tone: None)
        row.confirm.visible = False
        row.refresh(entries()[0], "My phone", SimpleNamespace(tone="signal", word="Linked", detail="Connected"), "iPhone")
        self.assertEqual(row.name.text(), "My phone")
        self.assertEqual(row.where.text(), "iPhone")
        self.assertFalse(row.directions.visible)
        self.assertFalse(row.direction_group.visible)
        row._ask(False)
        self.assertFalse(row.directions.visible)
        self.assertTrue(row.in_use.visible)
        self.assertTrue(row.remove_button.visible)

    def test_phone_waits_and_links_instead_of_showing_pair_again(self):
        for up, word in ((False, "Waiting"), (True, "Linked")):
            state = peers_view.describe(entries()[0], "My phone", up=up, driving=False,
                                        input_there=False, kind="", status="", waking=False)
            self.assertEqual(state.word, word)


if __name__ == "__main__":
    unittest.main()
