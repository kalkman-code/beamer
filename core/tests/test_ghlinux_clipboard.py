"""X11 stamp notifications against production methods with a fake GUI dispatcher."""

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


class X11Stamps(unittest.TestCase):
    def setUp(self):
        self.handlers = []
        self.own = False
        signal = NS(connect=self.handlers.append)
        self.clipboard = NS(dataChanged=signal)
        self.app = object()
        gui = NS(instance=lambda: self.app, clipboard=lambda: self.clipboard)
        self.ns = functions("clipboard_x11.py", {"change_stamp", "write_stamp", "_on_data_changed", "_connect",
                                                "_stamp", "_write"},
                            {"QGuiApplication": gui, "_connected_to": None, "_change_counter": 0,
                             "_synced_counter": None, "_write_stamp": None,
                             "Optional": Optional, "QMimeData": Mock, "_own_token": 0,
                             "OWN_FORMAT": "application/x-beamer-own", "PNG_MIME": "image/png",
                             "_is_own_write": lambda clipboard: self.own,
                             "_on_gui": lambda fn, *args, default: fn(*args) if self.app else default})

    def notify(self):
        for handler in self.handlers:
            handler()

    def test_first_stamp_observation_connects_local_copy_notifications(self):
        before = self.ns["change_stamp"]()
        self.notify()
        self.assertEqual(self.ns["change_stamp"](), before + 1)
        self.assertEqual(len(self.handlers), 1)

    def test_delayed_own_write_notification_does_not_look_like_a_local_copy(self):
        self.ns["_connect"]()
        self.ns.update(_synced_counter=0, _write_stamp=0)
        self.own = True
        cached_stamp = self.ns["write_stamp"]()
        self.notify()
        self.assertEqual(self.ns["change_stamp"](), self.ns["write_stamp"]())
        self.assertEqual(self.ns["change_stamp"](), cached_stamp)
        self.own = False
        self.notify()
        self.assertGreater(self.ns["change_stamp"](), self.ns["write_stamp"]())

    def test_failed_gui_access_reports_unknown_stamp(self):
        self.app = None
        self.assertIsNone(self.ns["change_stamp"]())

    def test_legacy_write_advances_once_with_sync_or_deferred_notifications(self):
        self.ns["change_stamp"]()
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                before = self.ns["change_stamp"]()
                self.ns["_synced_counter"] = before
                def publish(mime):
                    self.own = True
                    if not deferred:
                        self.notify()
                self.clipboard.setMimeData = publish
                written = self.ns["_write"]("remote", None)
                self.assertTrue(written)
                cached = self.ns["write_stamp"]()
                self.assertEqual(cached, before + 1)
                self.notify()
                self.assertEqual(self.ns["change_stamp"](), cached)
                self.own = False
                self.notify()
                self.assertEqual(self.ns["change_stamp"](), cached + 1)
                self.assertEqual(self.ns["write_stamp"](), cached)


if __name__ == "__main__":
    unittest.main()
