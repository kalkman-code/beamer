"""Synthetic settings fixtures exercising the actual app decisions without constructing UI."""

import ast
import copy
import importlib
import logging
from pathlib import Path
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Optional
from unittest import TestCase

from core import protocol, settings_sync, ways, peerlist
from core.tests.app_methods import load_methods

ROOT = Path(__file__).resolve().parents[2]
IDS = [protocol.id_text(bytes([value]) * 16) for value in (1, 2, 3)]
VALUES = dict(glow_style="beam", glow_colour="ocean", effect_length="long",
              effect_size="large", shortcut_arrival=False, shortcut_arrival_style="locator")


def mac_adapter():
    sys.path.insert(0, str(ROOT / "mac_app"))
    config = importlib.import_module("config")
    tree = ast.parse((ROOT / "mac_app/settings_store.py").read_text())
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "config_to_raw")
    namespace = {"config_module": config}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "settings_store.py", "exec"), namespace)
    return config, namespace["config_to_raw"]


def fixture(platform, ident):
    namespace = dict(settings_sync=settings_sync, protocol=protocol, peerlist=peerlist, time=time,
                     Optional=Optional, replace=replace, LOGGER=logging.getLogger("settings-behaviour"),
                     core_ways=ways, ways=ways)
    if platform == "mac":
        config, to_raw = mac_adapter()
        namespace.update(config_to_raw=to_raw, KEY_NAME_TO_CODE={"cmd_r", "f13"}, SettingsError=ValueError)
        names = ("same_state", "_apply_followed_design", "_receive_design", "apply_same", "_set_design_follow", "_set_same",
                 "_show_trigger_conflict", "_trigger_chosen")
        cls = load_methods("mac_app/kvm_bridge_app.py", "ControlWindow", names, namespace)
        app = cls()
        cfg = config.parse_config(dict(host="", port=24800, auth_token="", design_set_at=1000, design_by=ident))
        app.controller = SimpleNamespace(cfg=cfg)
        app.controller.apply_settings = lambda value: setattr(app.controller, "cfg", value)
        app.settings_store = SimpleNamespace(save=lambda raw: config.parse_config(copy.deepcopy(raw)))
        app.own_id = lambda: ident
        app.previews = None
        app._load_shared = lambda raw: None
        app.refresh = lambda: None
        app.values = lambda: settings_sync.mac_values(to_raw(app.controller.cfg))
        app.cfg = lambda: app.controller.cfg
        app.select = app._set_design_follow
        app.receive = lambda source, data: app.apply_same(data, peer=source)
        app.state = app.same_state
        app.style_hint = Label()
        app.conflict_note = app.style_hint
        app._changed = lambda: None
        app.key_recorder = SimpleNamespace(value=cfg.trigger_key)
        app.key_recorder.set_value = lambda value: setattr(app.key_recorder, "value", value)
        def record(key):
            app.key_recorder.value = key
            app._trigger_chosen(key)
        app.record = record
    else:
        sys.path.insert(0, str(ROOT / "win_app"))
        config = importlib.import_module("app_config")
        namespace.update(app_config=config, ConfigError=config.ConfigError, TRIGGER_KEYS={"cmd_r", "f13"})
        names = ("_same_state", "_apply_followed_design", "_on_settings", "_design_follow_chosen", "_set_same",
                 "_show_trigger_conflict", "_record_trigger")
        namespace.update(TRIGGER_KEYS=config.TRIGGER_KEYS, TRIGGER_VKS=config.TRIGGER_VKS,
                         UNRECORDABLE_TRIGGER_VKS=config.UNRECORDABLE_TRIGGER_VKS)
        cls = load_methods("win_app/kvm_bridge_win.py", "WindowsApplication", names, namespace)
        app = cls()
        app._config = config.Config(host="", port=24800, auth_token="", machine_id=ident,
                                    design_set_at=1000, design_by=ident)
        app._persist = lambda: True
        app._reflect_config = lambda cfg: None
        app.sender = SimpleNamespace(update_config=lambda cfg: None)
        app._configure_trigger = lambda cfg: None
        app._same_fields = lambda: ()
        app._design_fields = lambda: ()
        app.design_follow_choice = SimpleNamespace(currentData=lambda role: app.chosen)
        namespace["Qt"] = SimpleNamespace(ItemDataRole=SimpleNamespace(UserRole=0))
        app.values = lambda: settings_sync.pc_values(app._config)
        app.cfg = lambda: app._config
        def select(peer):
            app.chosen = peer
            app._design_follow_chosen(0)
        app.select = select
        app.receive = app._on_settings
        app.state = app._same_state
        app.trigger_note = Label()
        app.conflict_note = app.trigger_note
        app.trigger_recorder = SimpleNamespace(set_title=lambda title: None)
        app.record = lambda key: app._record_trigger("key", config.TRIGGER_VKS[key])
    app.logger = logging.getLogger("settings-behaviour")
    app.peers = [{"id": peer, "port": 24820} for peer in IDS if peer != ident]
    if platform == "mac":
        app.settings_store.current = lambda: {"peers": app.peers}
        app.controller.book = SimpleNamespace(peers=lambda: app.peers)
    else:
        app.book = SimpleNamespace(peers=lambda: app.peers)
    app._design_states = {}
    app._followed_version = None
    app._design_path = [ident]
    app._refresh_design_follow = lambda: None
    app._show_same = lambda: None
    app.sent = []
    app._send_same = lambda data=None, source=None: app.sent.append(copy.deepcopy(data if data is not None else app.state()))
    return app


class Label:
    def __init__(self):
        self.text = ""
        self.visible = True
        self.view = SimpleNamespace(setHidden_=lambda hidden: self.setVisible(not hidden))

    def set(self, text, **kwargs):
        self.text = text

    def setText(self, text):
        self.text = text

    def setVisible(self, visible):
        self.visible = visible


def reflect_shortcut_visibility(app, platform):
    """Stop at the next unrelated UI dependency, after the actual module visibility decision."""
    class UnrelatedUI(Exception):
        pass
    def stop(*args):
        raise UnrelatedUI
    def visibility(view, visible):
        view.visible = visible
    motion = SimpleNamespace(set_hidden=lambda view, hidden: visibility(view, not hidden), set_shown=visibility)
    if platform == "mac":
        pages = importlib.import_module("pages")
        namespace = dict(pages=SimpleNamespace(crossing_rows=pages.crossing_rows, part_names=stop), motion=motion)
        cls = load_methods("mac_app/kvm_bridge_app.py", "ControlWindow", ["_reflect"], namespace)
        app.method_boxes = {name: SimpleNamespace(value=name in app.cfg().crossing["methods"])
                            for name in ("shortcut", "edge", "corner", "part", "notch")}
        app.edge_row = app.parts_row = app.corner_row = SimpleNamespace()
        app.dragging_box = app.resistance_module = SimpleNamespace(view=SimpleNamespace())
        app.shortcut_module = SimpleNamespace(view=SimpleNamespace(visible=False))
        app.key_recorder.cancel = lambda: None
        app.edge_select = SimpleNamespace(value="right")
        method = lambda: cls._reflect(app)
        view = app.shortcut_module.view
    else:
        pages = importlib.import_module("pages_win")
        namespace = dict(pages_win=pages, ways=ways, motion=motion, TRIGGER_KEYS=SimpleNamespace(get=stop))
        cls = load_methods("win_app/kvm_bridge_win.py", "WindowsApplication", ["_reflect_ways"], namespace)
        app._crossing_settings = lambda: {"peers": [], "zones": []}
        app._crossing_machines = lambda: []
        app._chosen_machine = lambda: None
        app._rebuild_machine_choice = lambda values: None
        app._ways_in = lambda: set()
        ui = SimpleNamespace(
            set_value=lambda *args: None,
            set_names=lambda *args: None,
            setText=lambda *args: None,
            view=SimpleNamespace(setToolTip=lambda text: None),
        )
        for name in ("machine_choice", "edge_choice", "edge_heading", "edge_note", "edge_unlearned", "corner_choice", "parts_note"):
            setattr(app, name, ui)
        app.machine_row = app.resistance_module = SimpleNamespace()
        app.ways_module = SimpleNamespace(eyebrow=SimpleNamespace(setText=lambda text: None))
        app.shortcut_module = SimpleNamespace(visible=False)
        app.way_boxes = app.part_buttons = app.crossing_rows = {}
        method = lambda: cls._reflect_ways(app)
        view = app.shortcut_module
    try:
        method()
    except UnrelatedUI:
        pass
    return view.visible


def status_fixture(platform, caps, cached=False, following=False, offline=()):
    app = fixture(platform, IDS[1])
    app.peers = [{"id": IDS[0], "name": "Leader", "token": "synthetic"}]
    app.peers += [{"id": ident, "name": f"Offline {n}", "token": f"offline-{n}"} for n, ident in enumerate(offline)]
    if cached:
        app._design_states[IDS[0]] = {"set_at": 10, "by": IDS[0], "values": VALUES}
    app.cfg().design_follow_peer = IDS[0] if following else ""
    app._design_peer_caps = lambda: caps
    app._lock_followed_design = lambda entry, name: setattr(app, "locked_to", entry)
    namespace = dict(settings_sync=settings_sync, peerlist=peerlist, protocol=protocol)
    if platform == "mac":
        class MenuChoice:
            def __init__(self, choices, **kwargs):
                self.choices = choices
                self.items = []
                for value, title in choices:
                    cell = SimpleNamespace(enabled=True, tooltip="", setAlphaValue_=lambda alpha: None)
                    cell.setToolTip_ = lambda text, cell=cell: setattr(cell, "tooltip", text)
                    cell.setEnabled_ = lambda enabled, cell=cell: setattr(cell, "enabled", enabled)
                    self.items.append((value, cell))
                self.view = None
        namespace["widgets"] = SimpleNamespace(MenuChoice=MenuChoice, add=lambda *args, **kwargs: None)
        app.follow_host = SimpleNamespace(arrangedSubviews=lambda: [])
        app.follow_note = Label()
        app._set_design_follow = lambda peer: None
        method = load_methods("mac_app/kvm_bridge_app.py", "ControlWindow", ["_refresh_design_follow"], namespace)
        method._refresh_design_follow(app)
        app.status_titles = [title for value, title in app.follow_select.choices]
        app.status_tooltip = app.follow_select.items[1][1].tooltip if len(app.follow_select.items) > 1 else ""
        app.status_note = app.follow_note.text
    else:
        class Item:
            def __init__(self, title):
                self.title = title
            def setData(self, value, role):
                self.value = value
            def setEnabled(self, enabled):
                self.enabled = enabled
            def setToolTip(self, text):
                self.tooltip = text
        class Model:
            def __init__(self, choice):
                self.rows = []
            def appendRow(self, item):
                self.rows.append(item)
        namespace.update(QStandardItemModel=Model, QStandardItem=Item,
                         Qt=SimpleNamespace(ItemDataRole=SimpleNamespace(UserRole=0)))
        app._peer_entries = app.peers
        app._shown = lambda text: text
        choice = SimpleNamespace(blockSignals=lambda on: None, findData=lambda *args: 0,
                                 setCurrentIndex=lambda index: None)
        choice.setModel = lambda model: setattr(choice, "model", model)
        app.design_follow_choice = choice
        app.design_follow_note = Label()
        method = load_methods("win_app/kvm_bridge_win.py", "WindowsApplication", ["_refresh_design_follow"], namespace)
        method._refresh_design_follow(app)
        app.status_titles = [item.title for item in choice.model.rows]
        app.status_tooltip = getattr(choice.model.rows[1], "tooltip", "") if len(choice.model.rows) > 1 else ""
        app.status_note = app.design_follow_note.text
    return app


def leader_message(ident, stamp=100, values=None, path=None):
    state = dict(set_at=stamp, by=ident, values=dict(VALUES if values is None else values))
    if path is not None:
        state["path"] = path
    return settings_sync.message_data(False, 0, by=ident, design_state=state)


class SettingsBehaviourTests(TestCase):
    platform = None

    def setUp(self):
        self.app = fixture(self.platform, IDS[1])

    def cache_and_select(self, stamp=100):
        self.app.receive(IDS[0], leader_message(IDS[0], stamp))
        self.app.select(IDS[0])

    def assert_design(self, app, values=VALUES):
        self.assertEqual({key: app.values()[key] for key in settings_sync.DESIGN_KEYS}, values)

    def test_selecting_older_leader_adopts_all_six_values_and_announces_local_version(self):
        self.cache_and_select()
        self.assert_design(self.app)
        state = self.app.state()["design_sync"]
        self.assertGreater(state["set_at"], 1000)
        self.assertEqual(state["by"], IDS[1])
        self.assertEqual(state["path"], [IDS[0], IDS[1]])

    def test_compatible_design_not_yet_received_is_not_labelled_old(self):
        waiting = "Design not available yet: connect to this machine with Beamer 1.5.0 beta.6 or later."
        app = status_fixture(self.platform, {IDS[0]: {"settings", "design_sync"}})
        self.assertNotIn("Needs beta.6 or later", app.status_titles[1])
        self.assertEqual(app.status_titles[1], "Leader")
        self.assertEqual(app.status_tooltip, waiting)
        self.assertEqual(app.status_note, waiting)

    def test_connected_machine_with_design_is_offered_whatever_offline_peers_say(self):
        # 05-10-2026, 1.5.0-rc.4: the PC was linked and its Design had arrived, but the Mac's other
        # paired machines were offline and their missing Design set the note to "not available".
        app = status_fixture(self.platform, {IDS[0]: {"settings", "design_sync"}}, cached=True, offline=(IDS[2],))
        self.assertEqual(app.status_titles, ["None", "Leader"])
        self.assertEqual(app.status_tooltip, "")
        self.assertTrue(app.status_note.startswith("None: this "), app.status_note)

    def test_no_connected_machine_lists_none_with_one_quiet_line(self):
        app = status_fixture(self.platform, {}, offline=(IDS[2],))
        self.assertEqual(app.status_titles, ["None"])
        self.assertEqual(app.status_note, settings_sync.NO_MACHINE_CONNECTED)

    def test_observed_missing_design_capability_is_labelled_old(self):
        app = status_fixture(self.platform, {IDS[0]: {"settings"}})
        self.assertIn("Needs beta.6 or later", app.status_titles[1])

    def test_following_offline_machine_keeps_last_design_and_says_so(self):
        app = status_fixture(self.platform, {}, cached=True, following=True)
        self.assertEqual(app.status_note, "Keeping the last design received from Leader until it reconnects.")

    def test_shared_trigger_clash_preserves_local_jump_and_trigger_and_reports_conflict(self):
        self.app.peers.append({"id": IDS[0], "jump_key": "ctrl+f13"})
        before = self.app.cfg().trigger_key
        data = settings_sync.message_data(True, 20, {"trigger_key": "f13", "resistance_px": 75}, by=IDS[0])
        self.app.peers[0]["jump_key"] = "cmd+f13"
        self.app.receive(IDS[0], data)
        self.assertEqual(self.app.cfg().trigger_key, before)
        self.assertEqual(self.app.peers[0]["jump_key"], "cmd+f13")
        self.assertEqual(self.app.values()["resistance_px"], 75)
        self.assertIn("clashes", self.app.conflict_note.text)
        self.assertTrue(self.app.conflict_note.visible)

    def test_recording_a_trigger_clash_preserves_the_local_trigger_and_shows_a_note(self):
        self.app.peers.append({"id": IDS[0], "jump_key": "ctrl+f13"})
        before = self.app.cfg().trigger_key
        self.app.record("f13")
        self.assertEqual(self.app.cfg().trigger_key, before)
        self.assertIn("clashes", self.app.conflict_note.text)
        self.assertTrue(self.app.conflict_note.visible)
        if self.platform == "mac":
            self.assertEqual(self.app.key_recorder.value, before)

    def test_shared_clash_note_stays_visible_when_shortcut_is_switched_off(self):
        self.app.peers.append({"id": IDS[0], "jump_key": "ctrl+f13"})
        before = self.app.cfg().trigger_key
        self.app.receive(IDS[0], settings_sync.message_data(
            True, 20, {"trigger_key": "f13", "shortcut": False}, by=IDS[0]))
        self.assertEqual(self.app.cfg().trigger_key, before)
        self.assertIn("clashes", self.app.conflict_note.text)
        self.assertTrue(reflect_shortcut_visibility(self.app, self.platform))

    def test_next_leader_update_uses_leader_cursor_and_duplicates_do_not_resend(self):
        self.cache_and_select()
        changed = dict(VALUES, effect_length="short")
        message = leader_message(IDS[0], 101, changed)
        self.app.receive(IDS[0], message)
        self.assert_design(self.app, changed)
        count = len(self.app.sent)
        self.app.receive(IDS[0], message)
        self.app.receive(IDS[0], leader_message(IDS[0], 99))
        self.assertEqual(len(self.app.sent), count)
        self.assert_design(self.app, changed)

    def test_same_off_here_resumes_the_already_adopted_cached_version(self):
        self.cache_and_select()
        self.app.cfg().same_on_both = True
        self._set_values(dict(VALUES, effect_length="short"))
        self.app._set_same(False)
        self.assert_design(self.app)

    def test_same_off_received_resumes_the_already_adopted_cached_version(self):
        self.cache_and_select()
        self.app.cfg().same_on_both = True
        self._set_values(dict(VALUES, effect_length="short"))
        data = settings_sync.message_data(False, self.app.cfg().same_set_at + 1, by=IDS[2])
        self.app.receive(IDS[2], data)
        self.assert_design(self.app)

    def _set_values(self, values):
        self.set_values(self.app, values)

    def set_values(self, app, values):
        if self.platform == "mac":
            config, to_raw = mac_adapter()
            raw = settings_sync.apply_mac(to_raw(app.cfg()), values)
            app.controller.apply_settings(config.parse_config(raw))
        else:
            settings_sync.apply_pc(app.cfg(), values)

    def test_downstream_follower_adopts_when_its_leader_selects_older_design(self):
        downstream = fixture(self.platform, IDS[2])
        downstream.receive(IDS[1], self.app.state())
        downstream.select(IDS[1])
        self.cache_and_select()
        downstream.receive(IDS[1], self.app.state())
        self.assert_design(downstream)
        self.assertEqual(downstream.state()["design_sync"]["path"], IDS)

    def test_two_machine_cycle_has_bounded_announcements(self):
        self._cycle(2, False)

    def test_three_machine_cycle_has_bounded_announcements(self):
        self._cycle(3, False)

    def test_identical_values_cycle_has_bounded_announcements(self):
        self._cycle(3, True)

    def test_two_machine_identical_values_cycle_has_bounded_announcements(self):
        self._cycle(2, True)

    def _cycle(self, count, identical):
        apps = [fixture(self.platform, ident) for ident in IDS[:count]]
        if not identical:
            self.set_values(apps[0], VALUES)
        initial = [app.state() for app in apps]
        for index, app in enumerate(apps):
            leader = (index + 1) % count
            app.receive(IDS[leader], initial[leader])
            app.select(IDS[leader])
        queue = [(index, data) for index, app in enumerate(apps) for data in app.sent]
        for app in apps:
            app.sent.clear()
        processed = 0
        while queue:
            source, data = queue.pop(0)
            processed += 1
            self.assertLess(processed, 40, "follow cycle kept announcing")
            for index, app in enumerate(apps):
                if source != index:
                    app.receive(IDS[source], data)
                    queue.extend((index, message) for message in app.sent)
                    app.sent.clear()
        self.assertTrue(any(not app.cfg().design_follow_peer for app in apps))
