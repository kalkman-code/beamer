"""Pairing selection follows current discovery without opening a window or a socket."""

import unittest
from types import MethodType, SimpleNamespace
from unittest import mock

import kvm_bridge_app


class PairingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.window = SimpleNamespace(
            discovery=mock.Mock(error=None, pcs=mock.Mock(return_value=[])),
            chosen_pc=None, _pcs=[], _pcs_key=None, _pairing=False,
            pc_list=mock.Mock(arrangedSubviews=mock.Mock(return_value=[])),
            pc_frame=mock.Mock(), pc_empty=mock.Mock(), pair_status=mock.Mock(),
            confirm_button=mock.Mock(), code_boxes=mock.Mock(value=""),
            window=mock.Mock(), _find_answered=mock.Mock(), _pairing_finished=mock.Mock(),
            _shown=lambda text: text,
        )
        self.window.code_boxes.clear.side_effect = lambda: setattr(self.window.code_boxes, "value", "")
        self.window.pair_status.set.side_effect = lambda text, **kwargs: setattr(self, "status_text", text)
        self.status_text = ""
        for name in ("_refresh_pcs", "_choose_pc", "_say_choice", "_say_pairing", "confirmPair_"):
            method = getattr(kvm_bridge_app.ControlWindow, name)
            setattr(self.window, name, MethodType(getattr(method, "callable", method), self.window))
        for name in ("pressable", "stack", "LED", "Label", "hug", "squeeze", "pin", "add"):
            patcher = mock.patch.object(kvm_bridge_app.widgets, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def beacon(self, pair_id=None, reply_port=24821):
        return dict(name="TEST-PC", address="192.0.2.20", port=24820,
                    reply_port=reply_port, pair_id=pair_id)

    def refresh(self, pc):
        self.window.discovery.pcs.return_value = [pc] if pc is not None else []
        self.window._refresh_pcs()

    def choose(self, pc):
        self.refresh(pc)
        self.window._choose_pc(0)

    def enter(self, code):
        self.window.code_boxes.value = code

    def status(self):
        return self.status_text

    def test_selected_pc_starting_pairing_updates_hint_and_keeps_partial_code(self):
        self.choose(self.beacon())
        self.assertIn("not showing a code", self.status())
        self.enter("123")
        active = self.beacon("first-code")
        self.refresh(active)
        self.assertEqual(self.window.chosen_pc, active)
        self.assertEqual(self.status(), "Six digits, as shown on the PC.")
        self.assertEqual(self.window.code_boxes.value, "123")

    def test_selected_pc_stopping_pairing_updates_hint(self):
        self.choose(self.beacon("first-code"))
        self.refresh(self.beacon())
        self.assertIsNone(self.window.chosen_pc["pair_id"])
        self.assertIn("not showing a code", self.status())

    def test_active_code_rotation_updates_selected_and_submission_state(self):
        self.choose(self.beacon("first-code"))
        current = self.beacon("second-code")
        self.refresh(current)
        self.assertEqual(self.window.chosen_pc, current)
        self.assertEqual(self.window._pcs, [current])

    def test_reply_port_change_updates_selected_and_submission_state(self):
        self.choose(self.beacon("first-code"))
        current = self.beacon("first-code", reply_port=24822)
        self.refresh(current)
        self.assertEqual(self.window.chosen_pc, current)
        self.assertEqual(self.window._pcs, [current])

    def test_submit_uses_latest_pairing_id_and_reply_port(self):
        self.choose(self.beacon("first-code"))
        current = self.beacon("second-code", reply_port=24822)
        self.refresh(current)
        self.enter("123456")
        with mock.patch.object(kvm_bridge_app.threading, "Thread") as thread, \
                mock.patch.object(kvm_bridge_app.AppHelper, "callAfter"):
            self.window.confirmPair_(None)
            thread.assert_called_once()
            thread.call_args.kwargs["target"]()
        self.window.discovery.pair.assert_called_once_with(current, "123456")

    def test_pairing_progress_survives_beacon_changes(self):
        self.choose(self.beacon("first-code"))
        self.window._pairing = True
        self.window._say_pairing("Pairing with TEST-PC…")
        self.refresh(self.beacon())
        self.assertEqual(self.status(), "Pairing with TEST-PC…")

    def test_unchanged_beacon_preserves_error_message(self):
        pc = self.beacon("first-code")
        self.choose(pc)
        self.window._say_pairing("That code was not accepted.", "fault")
        self.refresh(dict(pc))
        self.assertEqual(self.status(), "That code was not accepted.")

    def test_refusal_stays_up_when_the_pc_drops_its_code(self):
        self.choose(self.beacon("first-code"))
        self.window._say_pairing("That code was not accepted.", "fault")
        self.refresh(self.beacon())
        self.assertEqual(self.status(), "That code was not accepted.")
        self.refresh(self.beacon("fresh-code"))
        self.assertEqual(self.status(), "Six digits, as shown on the PC.")

    def test_disappeared_pc_clears_selection_and_disables_submission(self):
        self.choose(self.beacon("first-code"))
        self.refresh(None)
        self.assertIsNone(self.window.chosen_pc)
        self.assertEqual(self.window._pcs, [])
        self.window.confirm_button.set_enabled.assert_called_with(False)

    def test_a_pc_on_another_version_of_the_exchange_is_said_plainly(self):
        pc = self.beacon("first-code")
        self.choose(pc)
        finished = kvm_bridge_app.ControlWindow._pairing_finished
        finished = MethodType(getattr(finished, "callable", finished), self.window)
        finished(pc, kvm_bridge_app.pairing.PairingError(kvm_bridge_app.pairing.ERROR_VERSION))
        self.assertEqual(
            self.status(),
            "The PC runs a different version of Beamer. Update Beamer on both machines, then pair again.",
        )


if __name__ == "__main__":
    unittest.main()
