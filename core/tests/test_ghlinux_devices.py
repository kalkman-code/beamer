"""Portal device replacement against production methods with fake input devices."""

import threading
from types import SimpleNamespace as NS
from typing import Optional
import unittest
from unittest.mock import Mock

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def functions(filename, names, namespace, owner=None):
    path = ROOT / "win_app" / filename
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if owner:
        body = next(node.body for node in body if isinstance(node, ast.ClassDef) and node.name == owner)
    selected = [node for node in body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class PortalReplacement(unittest.TestCase):
    def setUp(self):
        self.now = 1.0
        self.desktop = Mock()
        namespace = functions("capture_portal.py", {"_ei_event", "_tick", "_signal"},
                              {"Optional": Optional, "LOGGER": Mock(), "desktop_portal": self.desktop,
                               "DISABLED_SECONDS": 0.3, "HOME_AGAIN_SECONDS": 0.5,
                               "LANDING_SECONDS": 0.1}, "Hooks")
        hooks_type = type("Hooks", (), {name: namespace[name] for name in ("_ei_event", "_tick", "_signal")})
        self.hooks = h = hooks_type()
        self.pointer, self.keyboard = object(), object()
        h._devices = {id(self.pointer), id(self.keyboard)}
        h._leaving = set()
        h._removed_at = None
        h._session = NS(enabled=True)
        h._clock = lambda: self.now
        h._push_lock = threading.Lock()
        h._pending_push = None
        h._activation, h._press, h._away = 7, None, True
        h._away_wanted = lambda: h._away
        h._home = Mock()
        h._home_since = h._disabled_at = h._sent_home_at = None
        h._place_barriers = Mock()
        h._let_go_unannounced = Mock()

    def test_keyboard_replacement_across_batches_keeps_active_session(self):
        h = self.hooks
        h._ei_event(("device_removed", self.keyboard))
        self.now += 0.1
        h._tick()
        self.assertTrue(h._session.enabled)
        h._ei_event(("device_added", object()))
        self.now += 1
        h._tick()
        self.assertTrue(h._session.enabled)
        self.assertEqual(h._activation, 7)
        h._home.assert_not_called()

    def test_all_devices_replaced_in_next_batch_do_not_disable(self):
        h = self.hooks
        h._ei_event(("device_removed", self.keyboard))
        h._ei_event(("device_removed", self.pointer))
        self.now += 0.1
        h._tick()
        h._ei_event(("device_added", object()))
        self.now += 1
        h._tick()
        self.assertTrue(h._session.enabled)
        h._home.assert_not_called()

    def test_replacement_window_starts_when_the_last_device_is_removed(self):
        h = self.hooks
        h._ei_event(("device_removed", self.keyboard))
        self.now += 0.29
        h._ei_event(("device_removed", self.pointer))
        self.now += 0.02
        h._tick()
        self.assertTrue(h._session.enabled)
        h._ei_event(("device_added", object()))
        self.now += 1
        h._tick()
        h._home.assert_not_called()

    def test_gnome_cancel_without_replacement_brings_input_home_once(self):
        h = self.hooks
        h._ei_event(("device_removed", self.keyboard))
        h._ei_event(("device_removed", self.pointer))
        self.now += 0.31
        h._tick()
        self.assertFalse(h._session.enabled)
        self.assertIsNone(h._activation)
        h._home.assert_called_once()
        h._tick()
        h._home.assert_called_once()

    def test_expected_barrier_reconfiguration_removals_do_not_cancel(self):
        h = self.hooks
        h._leaving = set(h._devices)
        h._ei_event(("device_removed", self.keyboard))
        h._ei_event(("device_removed", self.pointer))
        self.now += 1
        h._tick()
        self.assertTrue(h._session.enabled)
        h._home.assert_not_called()

    def test_explicit_disabled_still_releases_input_immediately(self):
        h = self.hooks
        h._expect_disabled = False
        h._signal("Disabled", {})
        self.assertFalse(h._session.enabled)
        self.assertIsNone(h._activation)
        h._home.assert_called_once()

    def test_explicit_disable_clears_an_earlier_unconfirmed_removal(self):
        h = self.hooks
        h._expect_disabled = False
        h._ei_event(("device_removed", self.keyboard))
        h._ei_event(("device_removed", self.pointer))
        h._signal("Disabled", {})
        self.assertIsNone(h._removed_at)


if __name__ == "__main__":
    unittest.main()
