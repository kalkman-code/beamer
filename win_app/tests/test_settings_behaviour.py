"""Windows settings decisions with in-memory configuration and no Qt window."""

from core.tests.settings_behaviour import SettingsBehaviourTests as _SettingsBehaviourTests
from core.tests.app_methods import load_methods
from core import settings_sync, peerlist
from types import SimpleNamespace
from unittest import TestCase


class WindowsSettingsBehaviourTests(_SettingsBehaviourTests):
    platform = "win"

    def setUp(self):
        super().setUp()
        self.app.parts_choice = SimpleNamespace(view=SimpleNamespace(refresh_metrics=lambda: None))


del _SettingsBehaviourTests


class SameCompatibilityTests(TestCase):
    def test_old_peer_status_disables_same_but_an_unobserved_peer_does_not(self):
        namespace = dict(settings_sync=settings_sync, peerlist=peerlist,
                         widgets=SimpleNamespace(set_role=lambda note, role: setattr(note, "role", role)))
        cls = load_methods("win_app/kvm_bridge_win.py", "WindowsApplication", ["_show_same"], namespace)
        for capabilities, enabled, checked, old in (({}, True, True, False),
                                                    ({"new": {"settings"}}, True, True, False),
                                                    ({"old": set()}, False, False, True),
                                                    ({"new": {"settings"}, "old": set()}, False, False, True)):
            with self.subTest(capabilities=capabilities):
                app = cls()
                app._config = SimpleNamespace(same_on_both=True)
                app._paired = True
                app._peer_caps = lambda: capabilities
                switch = SimpleNamespace(checked=True, enabled=True, blockSignals=lambda on: None)
                switch.isChecked = lambda: switch.checked
                switch.setChecked = lambda on: setattr(switch, "checked", on)
                switch.setEnabled = lambda on: setattr(switch, "enabled", on)
                app.same_switch = switch
                app.same_note = SimpleNamespace(
                    setText=lambda text: setattr(app.same_note, "text", text),
                    setToolTip=lambda text: setattr(app.same_note, "tool_tip", text),
                )
                app._peer_entries = [{"token": "desktop", "name": "Desktop", "port": 24820}]
                app._shown = lambda text: text
                app.scope_labels = {}
                app.own_notes = []
                app._refresh_design_follow = lambda: None
                app._show_same()
                self.assertEqual(switch.enabled, enabled)
                self.assertEqual(switch.checked, checked)
                self.assertEqual("too old" in app.same_note.text, old)
                self.assertEqual(app.same_note.role, "note-amber" if old else "note")
