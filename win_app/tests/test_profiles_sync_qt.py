"""Qt app wire paths with synthetic settings and links; no input capture or GUI is started."""

import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app_config
from core import crossing_state, profiles, protocol, settings_sync
from core.tests import responder_harness as h

try:
    import kvm_bridge_win as kvm
except ImportError:
    kvm = None


@unittest.skipIf(kvm is None, "needs PySide6 and the Qt app dependencies")
class ProfilesSyncQtTests(unittest.TestCase):
    def setUp(self):
        self.own, self.a, self.b, self.old = [protocol.id_text(peer) for peer in (h.HERE, h.B, h.C, bytes([4]) * 16)]
        self.settings = app_config.migrate(None, machine_id=self.own)
        self.settings["peers"] = [h.entry(h.B, "Studio", side="right"), h.entry(h.C, "Laptop", side="left")]
        self.settings = crossing_state.migrate(self.settings, "pc")["settings"]
        self.settings, _ = profiles.before_load(self.settings, now=100)
        self.settings, _ = profiles.after_load(self.settings, platform="pc", now=100)
        self.config = app_config.config_from_dict(app_config._flat_from_settings(self.settings))
        self.envelope = dict(on=False, set_at=100, by=self.own, groups=[], values={"shortcut": True},
                             layout={"tiles": {}}, design_sync={"values": {}})
        self.caps = {h.B: {"settings", "layout", "profiles"}, h.C: {"settings", "layout", "profiles"},
                     bytes([4]) * 16: {"settings", "layout"}}
        self.app = SimpleNamespace(
            config_path=Path("synthetic-settings.json"), _config=self.config,
            _profile_last_heard={}, _peer_caps=lambda: self.caps, _same_state=lambda: self.envelope,
            _send_to=mock.Mock(return_value=True), _live=lambda peer: True,
            _own_displays=lambda: {}, _layout_capable=lambda: {self.own, self.a, self.b},
            _crossing_event_args=lambda: dict(platform="pc", capable={self.own, self.a, self.b},
                                             live=lambda peer: True, gone=lambda peer: False, now=101),
            book=SimpleNamespace(peers=lambda: self.settings["peers"]),
        )
        self.app._desk_messages = lambda desk, caps: kvm.WindowsApplication._desk_messages(self.app, desk, caps)
        self.app._publish_desk = lambda settings=None: kvm.WindowsApplication._publish_desk(self.app, settings)
        # What linked peers were last told, as after the app's first publish at start.
        self.app._desk_published = profiles.desk_message(self.settings)

    def test_framed_desk_is_sent_only_to_profiles_peers_and_never_with_layout_or_design(self):
        outcome = crossing_state.set_same_ways(self.settings, True, platform="pc", now=101)
        with mock.patch.object(profiles, "MAX_DESK_BYTES", 200):
            kvm.WindowsApplication._carry_desk(self.app, outcome)
        calls = self.app._send_to.call_args_list
        self.assertGreater(len(calls), 2)
        self.assertEqual({call.args[0] for call in calls}, {h.B, h.C})
        for call in calls:
            data = call.args[1]["data"]
            self.assertIn("desk", data)
            self.assertNotIn("layout", data)
            self.assertNotIn("design_sync", data)
            self.assertNotIn("values", data)
            self.assertEqual(data["set_at"], self.envelope["set_at"])

    def test_a_change_made_outside_a_crossing_event_still_reaches_linked_peers(self):
        # App review: a removal (or pairing) changed this machine's status and no event carried it.
        changed = crossing_state.set_same_ways(self.settings, True, platform="pc", now=101)["settings"]
        kvm.WindowsApplication._publish_desk(self.app, changed)
        peers = {call.args[0] for call in self.app._send_to.call_args_list}
        self.assertEqual(peers, {h.B, h.C}, "profiles peers only")
        self.app._send_to.reset_mock()
        kvm.WindowsApplication._publish_desk(self.app, changed)
        self.app._send_to.assert_not_called()

    def test_unchanged_outcome_forwards_away_from_source_and_answers_source_only(self):
        full = profiles.desk_message(self.settings)
        forward = {"on": full["on"]}
        answer = {"active": full["active"]}
        outcome = dict(settings=self.settings, send_desk=[], forward=forward, answer=answer)
        kvm.WindowsApplication._carry_desk(self.app, outcome, source=self.a)
        sent = {peer: message["data"]["desk"] for peer, message in (call.args for call in self.app._send_to.call_args_list)}
        self.assertEqual(sent, {h.B: answer, h.C: forward})

    def test_link_up_returns_whole_framed_desk(self):
        self.app._carry_crossing = mock.Mock()
        self.app._send_same = mock.Mock()
        with mock.patch.object(app_config, "load_settings", return_value=copy.deepcopy(self.settings)), \
                mock.patch.object(profiles, "MAX_DESK_BYTES", 200):
            messages = kvm.WindowsApplication._announce(self.app, h.B)
            held = self.app._carry_crossing.call_args.args[0]["settings"]
            expected = profiles.frames(profiles.desk_message(held))
        frames = [message["data"]["desk"] for message in messages if "desk" in message.get("data", {})]
        self.assertEqual(frames, expected)
        self.assertGreater(len(frames), 1)
        paths = {path for frame in frames for path, register in profiles._paths(frame)}
        self.assertEqual(paths, {path for path, register in profiles._paths(profiles.desk_message(held))})
        self.assertIsInstance(self.app._profile_last_heard[self.a], (int, float))

    def test_received_desk_uses_crossing_event_path_and_marks_current_link_heard(self):
        self.app._carry_crossing = mock.Mock()
        self.app._design_states = {}
        self.app._profile_last_heard[self.a] = 100
        incoming = settings_sync.desk_message_data(self.envelope, profiles.desk_message(self.settings))
        outcome = crossing_state.desk_arrived(self.settings, incoming["desk"], source=self.a,
                                               platform="pc", linked=True, now=101)
        with mock.patch.object(app_config, "load_settings", return_value=copy.deepcopy(self.settings)), \
                mock.patch.object(settings_sync, "arrived", return_value=None), \
                mock.patch.object(settings_sync, "names_arrived", return_value=({}, None)), \
                mock.patch.object(settings_sync, "merge", return_value=None), \
                mock.patch.object(crossing_state, "desk_arrived", return_value=outcome) as arrived:
            kvm.WindowsApplication._on_settings(self.app, self.a, incoming)
        self.assertEqual(arrived.call_args.kwargs["source"], self.a)
        self.assertTrue(arrived.call_args.kwargs["linked"])
        # What arrives is read first: read_desk_state keeps only stamped registers, so an unset
        # tombstone ("removed": None) is not passed on.
        self.assertEqual(arrived.call_args.args[1], profiles.read_desk_state(incoming))
        self.app._carry_crossing.assert_called_once_with(outcome, source=self.a)
        self.assertIsNone(self.app._profile_last_heard[self.a])

    def test_shake_notice_uses_local_count_and_stops_after_three(self):
        settings = copy.deepcopy(self.settings)
        self.app._label_of = lambda peer: "Studio"
        self.app._on_alert = mock.Mock()
        with mock.patch.object(app_config, "load_settings", side_effect=lambda path: copy.deepcopy(settings)), \
                mock.patch.object(app_config, "write_settings", side_effect=lambda path, saved: settings.update(saved)):
            for _ in range(5):
                kvm.WindowsApplication._on_shake(self.app, h.B)
        self.assertEqual(settings["shake_notices"], 3)
        self.assertEqual(self.app._on_alert.call_count, 3)
        self.assertIn("Shook home from Studio.", self.app._on_alert.call_args.args[1])

    def test_startup_configures_shake_before_input_hooks_start(self):
        events = []
        self.app._any_send = lambda: True
        self.app._configure_trigger = lambda config: events.append("trigger")
        self.app._configure_shake = lambda: events.append("shake")
        self.app.hooks = SimpleNamespace(start=lambda: events.append("hooks"))
        self.app.sender = SimpleNamespace(start=lambda config: events.append("sender"))
        kvm.WindowsApplication._start_sending(self.app, self.config)
        self.assertEqual(events, ["trigger", "shake", "hooks", "sender"])

    def test_rejected_desk_cannot_change_settings_or_forward_registers(self):
        settings = copy.deepcopy(self.settings)
        settings["peers"][0]["allow_drive"] = False
        outcome = crossing_state.desk_arrived(settings, profiles.desk_message(self.settings),
                                              source=self.a, platform="pc", linked=True, now=101)
        self.assertFalse(outcome["accepted"])
        self.assertEqual(outcome["reason"], "untrusted")
        self.assertEqual(outcome["settings"], settings)
        kvm.WindowsApplication._carry_desk(self.app, outcome, source=self.a)
        self.app._send_to.assert_not_called()

    def test_shake_settings_reach_sender_without_resetting_an_unchanged_detector(self):
        self.app.sender = SimpleNamespace(set_shake=mock.Mock(), set_escape_home=mock.Mock())
        self.app.bridge = SimpleNamespace(shook_home=SimpleNamespace(emit=mock.Mock()),
                                          escaped_home=SimpleNamespace(emit=mock.Mock()))
        self.app._shake_enabled = None
        self.app._shake_sensitivity = None
        self.app._escape_enabled = None
        kvm.WindowsApplication._configure_shake(self.app, self.settings)
        kvm.WindowsApplication._configure_shake(self.app, self.settings)
        self.assertEqual(self.app.sender.set_shake.call_count, 1)
        self.assertTrue(self.app.sender.set_shake.call_args.args[0])
        changed = crossing_state.set_shake(self.settings, False, platform="pc", now=102)["settings"]
        kvm.WindowsApplication._configure_shake(self.app, changed)
        self.assertEqual(self.app.sender.set_shake.call_count, 2)
        self.assertFalse(self.app.sender.set_shake.call_args.args[0])
        self.assertEqual(self.app.sender.set_shake.call_args.kwargs["on_shake"], self.app.bridge.shook_home.emit)
        # Triple Escape is this machine's own setting, on unless turned off, and set once.
        self.app.sender.set_escape_home.assert_called_once_with(True, on_escape=self.app.bridge.escaped_home.emit)
        kvm.WindowsApplication._configure_shake(self.app, {**changed, "escape_home": False})
        self.assertEqual(self.app.sender.set_escape_home.call_args.args, (False,))


class ProfilesLoadNotesTests(unittest.TestCase):
    def test_older_version_notice_is_exposed_once_without_a_settings_flag(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            path = Path(folder) / "settings.json"
            own, peer = protocol.id_text(h.HERE), protocol.id_text(h.B)
            settings = app_config.migrate(None, machine_id=own)
            settings["peers"] = [h.entry(h.B, "Studio", side="right")]
            app_config.write_settings(path, settings)
            settings = app_config.load_settings(path)
            settings["peers"][0]["ways"]["edge"] = not settings["peers"][0]["ways"]["edge"]
            app_config.write_settings(path, settings)
            loaded = app_config.load_settings(path)
            self.assertEqual(app_config.load_notes(path), ["older_version"])
            self.assertEqual(app_config.load_notes(path), [])
            app_config.load_settings(path)
            self.assertEqual(app_config.load_notes(path), [])
            self.assertNotIn("profile_load_notes", loaded)


if __name__ == "__main__":
    unittest.main()
