"""Windows to Windows (WIRE.md section 7, same family): every physical modifier pressed on one PC is
pressed as the same key on the other, over real links, in either style and in either direction. The
press goes from the capture's name for the key, through LinkSender and the key table, over a link
to a LinkResponder, and is checked against the key the Windows injector would press for the name
that arrived."""

import unittest

from links_rig import make_config

import capture_win
import input_injector
import sender
from core import protocol, receiver
from core.return_edge import Rect
from core.tests import responder_harness as harness
from core.tests.responder_harness import B, HERE, TOKENS, Machine, entry, wait_for

MONITORS = [Rect(0, 0, 1920, 1080)]
MODIFIER_VKS = {
    0xA0: "Left Shift", 0xA1: "Right Shift", 0xA2: "Left Ctrl", 0xA3: "Right Ctrl",
    0x5B: "Left Windows", 0x5C: "Right Windows", 0xA4: "Left Alt", 0xA5: "Right Alt or AltGr",
}


class _Pair:
    """`near` drives `far`, both Windows."""

    near, far = B, HERE

    def setUp(self):
        token = TOKENS[B]
        self.machine = Machine([entry(self.near, "Near", platform="windows", token=token)], own=self.far, name="Far").start()
        self.addCleanup(self.machine.stop)
        peer = entry(self.far, "Far", token=token, host="127.0.0.1", port=self.machine.port, side="left", linked=False,
                     platform="windows")
        self.settings = harness.Settings([peer], [{"peer": protocol.id_text(self.far), "kind": "edge"}])
        self.settings.data["machine_id"] = protocol.id_text(self.near)
        book = receiver.PeerBook(self.settings.load, self.settings.save)
        self.sender = sender.LinkSender(
            book,
            lambda: {"id": self.near, "name": "Near", "platform": "windows", "app": "1.5.0", "caps": list(harness.CAPS), "port": 24820},
            zones=lambda: self.settings.data["zones"],
            desktop=harness.FakeDesktop(MONITORS, (0, 500)),
            clipboard=harness.FakeClipboard("near"),
            is_local=lambda host: False,
            arrival_callback=lambda edge, x, y: None,
            status_callback=lambda connected, detail: None,
        )
        self.sender.on_alert = lambda title, message: None

    def start(self, style):
        self.sender.start(make_config(machine_id=protocol.id_text(self.near), modifier_style=style))
        self.addCleanup(self.sender.stop)
        self.assertTrue(wait_for(lambda: self.sender.connected, 5), self.sender.status)
        self.assertTrue(self.sender.set_redirecting(True))
        self.assertTrue(wait_for(lambda: self.machine.owners and self.machine.owners[-1] == self.near))

    def pressed_there(self, style):
        self.start(style)
        for vk in MODIFIER_VKS:
            name = capture_win.VK_TO_NAME[vk]
            self.sender.on_key(name, True, vk)
            self.sender.on_key(name, False, vk)
        self.assertTrue(wait_for(lambda: len([c for c in self.machine.injected() if c[0] == "key"]) == 2 * len(MODIFIER_VKS)))
        keys = [call for call in self.machine.injected() if call[0] == "key"]
        return [(input_injector.VK_MAP[name], down) for _, name, down in keys]

    def check(self, style):
        expected = [(vk, down) for vk in MODIFIER_VKS for down in (True, False)]
        got = self.pressed_there(style)
        self.assertEqual(got, expected, [(MODIFIER_VKS.get(vk, hex(vk)), down) for vk, down in got])

    def test_every_modifier_arrives_as_itself_in_the_semantic_style(self):
        self.check("semantic")

    def test_every_modifier_arrives_as_itself_in_the_positional_style(self):
        self.check("positional")


class NearDrivesFar(_Pair, unittest.TestCase):
    near, far = B, HERE


class FarDrivesNear(_Pair, unittest.TestCase):
    near, far = HERE, B


if __name__ == "__main__":
    unittest.main()
