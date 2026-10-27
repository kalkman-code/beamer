import unittest
from unittest import mock

from core import keyboard_sync, protocol
from core.tests.responder_harness import B, C, HERE, Initiator, Machine, entry


def key(name, down=True):
    return {"type": "keydown" if down else "keyup", "data": {"key": name}}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.language = "ru"
        self.backend = mock.Mock()
        self.backend.current_language.side_effect = lambda: self.language
        self.backend.select_language.side_effect = self.select
        self.sender = keyboard_sync.Outbound(self.backend)

    def select(self, language):
        self.language = language
        return True

    def send(self, message, **kwargs):
        return self.sender.messages(B, message, enabled=kwargs.get("enabled", True),
                                    caps=kwargs.get("caps", {"input_language"}))

    def test_opt_in_and_peer_capability_are_both_required(self):
        message = key("ю")
        for options in ({"enabled": False}, {"caps": set()}):
            self.assertEqual(self.send(message, **options), [message])
        self.backend.current_language.assert_not_called()

    def test_language_goes_with_focus_and_typing_without_changing_the_original(self):
        focus = protocol.focus_v6(1, HERE)
        for message in (focus, key("ю"), key("ю", False)):
            self.assertEqual(self.send(message)[0]["data"]["input_language"], "ru")
            self.assertNotIn("input_language", message["data"])

    def test_bare_alt_shift_is_one_language_change_and_no_remote_modifiers(self):
        result = []
        for message in (key("alt"), key("shift"), key("shift", False), key("alt", False)):
            result += self.send(message)
        self.assertEqual(result, [{"type": "input_language", "data": {"language": "en"}}])
        self.backend.select_language.assert_called_once_with("en")
        self.assertEqual(self.send(key("a"))[0]["data"]["input_language"], "en")

    def test_altgr_shift_typing_alt_tab_ctrl_alt_shift_and_mouse_keep_their_events(self):
        cases = [
            [key("alt_r"), key("shift"), key("@"), key("@", False), key("shift", False), key("alt_r", False)],
            [key("alt"), key("tab"), key("tab", False), key("alt", False)],
            [key("ctrl"), key("alt"), key("shift"), key("shift", False), key("alt", False), key("ctrl", False)],
            [key("alt"), {"type": "mousedown", "data": {"button": "left"}}, key("alt", False)],
        ]
        for messages in cases:
            with self.subTest(messages=messages):
                self.sender.forget(B)
                result = [part for message in messages for part in self.send(message)]
                for part in result:
                    part["data"].pop("input_language", None)
                self.assertEqual(result, messages)
        self.backend.select_language.assert_not_called()

    def test_missing_layout_preserves_the_original_chord(self):
        self.backend.select_language.side_effect = lambda language: False
        messages = [key("alt"), key("shift"), key("alt", False), key("shift", False)]
        self.assertEqual([part for message in messages for part in self.send(message)], messages)

    def test_disconnection_discards_buffered_modifiers(self):
        self.assertEqual(self.send(key("alt")), [])
        self.sender.forget(B)
        self.assertEqual(len(self.send(key("a"))), 1)


class ProtocolTests(unittest.TestCase):
    def test_language_extension_is_validated(self):
        message = key("ю")
        message["data"].update(seq=1, input_language="ru")
        self.assertEqual(protocol.read_input(message)["input_language"], "ru")
        for invalid in (None, [], {}, True, "xx", "ru-RU"):
            message["data"]["input_language"] = invalid
            self.assertIsNone(protocol.read_input(message))

    def test_explicit_language_event_is_acknowledgeable_input(self):
        message = {"type": "input_language", "data": {"language": "en", "seq": 2}}
        self.assertEqual(protocol.read_input(message), {"type": "input_language", "seq": 2, "input_language": "en"})

    def test_let_go_ignores_language_but_take_validates_it(self):
        message = protocol.focus_v6(1, HERE)
        message["data"]["input_language"] = "ru"
        self.assertEqual(protocol.read_focus(message, HERE)["input_language"], "ru")
        message["data"]["input_language"] = "invalid"
        self.assertTrue(protocol.read_focus(message, HERE)["malformed"])
        self.assertNotIn("input_language", protocol.read_focus(message, B))


class ReceiverTests(unittest.TestCase):
    def test_only_the_current_owner_can_change_the_layout_and_disabled_receivers_ignore_it(self):
        machine = Machine([entry(B, "Bee"), entry(C, "Sea")], caps={"input_language"}).start()
        self.addCleanup(machine.stop)
        changed = []
        machine.injector.sync_keyboard_language = lambda language: (changed.append(language), True)[1]
        owner = Initiator(machine, B)
        self.addCleanup(owner.close)
        owner.handshake(owner.hello(caps=["input_language"]))
        other = Initiator(machine, C)
        self.addCleanup(other.close)
        other.handshake(other.hello(caps=["input_language"]))
        self.assertEqual(owner.answer(owner.take())[0], protocol.MSG_ACCEPT)
        event = {"type": "input_language", "data": {"language": "ru"}}
        self.assertIsNotNone(owner.acked(owner.input(event)))
        self.assertEqual(changed, [])
        machine.responder.keyboard_sync = True
        self.assertIsNotNone(other.acked(other.input(event)))
        self.assertEqual(changed, [])
        self.assertIsNotNone(owner.acked(owner.input(event)))
        self.assertEqual(changed, ["ru"])
        message = key("ю")
        message["data"]["input_language"] = "ru"
        self.assertIsNotNone(owner.acked(owner.input(message)))
        self.assertEqual(changed, ["ru", "ru"])
        self.assertIn(("key", "ю", True), machine.injected())

    def test_a_missing_layout_or_native_failure_does_not_retry_on_every_key(self):
        machine = Machine([entry(B, "Bee")])
        machine.injector.sync_keyboard_language = mock.Mock(side_effect=OSError("unavailable"))
        with self.assertLogs("core.receiver", level="ERROR"):
            self.assertFalse(machine.responder._sync_keyboard_language("ru"))
        self.assertFalse(machine.responder._sync_keyboard_language("ru"))
        machine.injector.sync_keyboard_language.assert_called_once_with("ru")


if __name__ == "__main__":
    unittest.main()
