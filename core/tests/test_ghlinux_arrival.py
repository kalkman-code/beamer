"""Linux regressions against production methods, without loading Qt or native input modules."""

import ast
from pathlib import Path
import threading
from types import SimpleNamespace as NS
import unittest
from typing import Optional
from unittest.mock import Mock

from core import return_edge

ROOT = Path(__file__).resolve().parents[2]


def functions(filename, names, namespace, owner=None):
    path = ROOT / "win_app" / filename
    body = ast.parse(path.read_text(encoding="utf-8")).body
    if owner:
        body = next(node.body for node in body if isinstance(node, ast.ClassDef) and node.name == owner)
    selected = [node for node in body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class WaylandArrival(unittest.TestCase):
    def setUp(self):
        self.methods = functions("sender.py", {"zone_models", "_press_edge"},
                                 {"return_edge": return_edge, "LOGGER": Mock()}, "LinkSender")
        self.model = return_edge.PartEdge("left", ["middle"], 10)
        self.sender = NS(_lock=threading.RLock(), _owner=object(), driven=lambda: True,
                         _refresh_display_peers=Mock(), _display_peer_models=[],
                         _represented_displays=lambda: set(), edges_held=False,
                         _models=[(b"peer", self.model)], receiving_split=lambda: False,
                         _home_hold=None, push_out=Mock(), _buttons_held=set(),
                         _desktop_module=Mock(side_effect=AssertionError("local crossing was evaluated")))

    def test_arrival_removes_overlapping_local_barriers_until_the_visit_ends(self):
        self.assertEqual(self.methods["zone_models"](self.sender), [])
        self.sender.driven = lambda: False
        self.assertEqual(self.methods["zone_models"](self.sender), [self.model])

    def test_a_stale_captured_move_cannot_start_the_links_home_thread(self):
        self.methods["_press_edge"](self.sender, -120, 0)
        self.sender._desktop_module.assert_not_called()

    def test_receiver_return_still_holds_arrival_then_crosses_deliberately(self):
        now = [1.0]
        model = return_edge.PartEdge("left", ["middle"], 10, clock=lambda: now[0])
        model.start_grace()
        screen = [return_edge.Rect(0, 0, 100, 100)]
        now[0] += 0.003
        self.assertEqual(model.feed(screen, (0, 50), -120, 0).action, return_edge.HOLD)
        now[0] += return_edge.ARRIVAL_GRACE_SECONDS
        self.assertEqual(model.feed(screen, (0, 50), -120, 0).action, return_edge.CROSS)

    def test_native_local_mouse_can_still_take_over_while_driven(self):
        s = self.sender
        s.push_out = None
        screen = return_edge.Rect(0, 0, 100, 100)
        s._desktop_module = lambda: NS(cursor_position=lambda: (0, 50))
        s._cached_monitors = lambda: [screen]
        s._cached_display_map = lambda: {}
        s._go, s._notify_pressure = Mock(), Mock()
        self.methods["_press_edge"](s, -120, 0)
        s._go.assert_called_once_with(b"peer", "right", 0.5, None, None)

    def test_wayland_local_crossing_returns_after_the_peer_lets_go(self):
        self.sender.driven = lambda: False
        screen = return_edge.Rect(0, 0, 100, 100)
        self.sender._desktop_module = lambda: NS(cursor_position=lambda: (0, 50))
        self.sender._cached_monitors = lambda: [screen]
        self.sender._cached_display_map = lambda: {}
        self.sender._go, self.sender._notify_pressure = Mock(), Mock()
        self.methods["_press_edge"](self.sender, -120, 0)
        self.sender._go.assert_called_once_with(b"peer", "right", 0.5, None, None)


if __name__ == "__main__":
    unittest.main()
