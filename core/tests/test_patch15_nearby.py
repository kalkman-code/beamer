"""Exercise native refresh invalidation without importing AppKit or opening a window."""

import ast
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]


class NearbyRefresh(unittest.TestCase):
    def setUp(self):
        path = ROOT / "mac_app" / "machines_panel.py"
        body = ast.parse(path.read_text(encoding="utf-8")).body
        owner = next(node for node in body if isinstance(node, ast.ClassDef) and node.name == "MachinesPanel")
        method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "_refresh_nearby")
        self.rows, self.labels = [], []
        def row(*args, **kwargs):
            result = Mock()
            self.rows.append(result)
            return result
        def label(*args, **kwargs):
            result = Mock()
            self.labels.append(result)
            return result
        ns = {"tr": lambda text: text, "seconds": str, "grouped": lambda text: text,
              "pairing": NS(CODE_DIGITS=6), "machines": NS(platform_name=lambda name: name),
              "AppKit": NS(NSAccessibilityRadioButtonRole="radio"),
              "theme": NS(TYPE={"note": 12}), "widgets": NS(pressable=row, Label=label, pin=Mock())}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), ns)
        self.refresh = ns["_refresh_nearby"]
        self.panel = p = NS(service=NS(nearby_status={"state": "choosing", "peers": [
            {"name": "Test PC", "platform": "Windows", "address": "192.0.2.40"}]}),
            controller=NS(cfg=NS(hide_addresses=False)), _nearby_key=None,
            _nearby_widgets=Mock(), _shown=lambda value: value)
        for name in ("nearby_status_label", "nearby_expiry", "nearby_sas", "nearby_peer_label",
                     "nearby_confirm", "nearby_cancel", "nearby_view", "nearby_choices"):
            setattr(p, name, Mock())
        p.nearby_choices.arrangedSubviews.return_value = []

    def test_first_candidate_invalidates_label_and_row_then_lays_out_the_subtree(self):
        self.refresh(self.panel)
        self.assertEqual(len(self.rows), 1)
        self.labels[0].view.setNeedsDisplay_.assert_called_once_with(True)
        self.rows[0].setNeedsLayout_.assert_called_once_with(True)
        self.rows[0].setNeedsDisplay_.assert_called_once_with(True)
        self.panel.nearby_choices.setNeedsLayout_.assert_called_once_with(True)
        self.panel.nearby_view.setNeedsLayout_.assert_called_once_with(True)
        self.panel.nearby_view.layoutSubtreeIfNeeded.assert_called_once_with()

    def test_unchanged_candidates_do_not_rebuild_or_relayout(self):
        self.refresh(self.panel)
        self.refresh(self.panel)
        self.assertEqual(len(self.rows), 1)
        self.panel.nearby_view.layoutSubtreeIfNeeded.assert_called_once_with()

    def test_expiry_clears_choices_and_relayouts(self):
        self.refresh(self.panel)
        self.panel.service.nearby_status = {"state": "expired"}
        self.refresh(self.panel)
        self.assertEqual(self.panel.nearby_view.layoutSubtreeIfNeeded.call_count, 2)


if __name__ == "__main__":
    unittest.main()
