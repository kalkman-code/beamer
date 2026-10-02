"""What each machine's row on Overview says, from what the link layer knows of it. Pure."""

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import peers_view
from core import protocol

GOOD = protocol.id_text(bytes(range(32)))
PEER_ID = protocol.id_text(b"\x01" * 16)


def entry(**fields):
    base = {"id": PEER_ID, "name": "Studio Mac", "platform": "macos", "token": GOOD, "host": "192.168.77.5",
            "port": 24820, "send": True, "allow_drive": True, "linked": True}
    base.update(fields)
    return base


def state(peer=None, **link):
    args = dict(up=False, driving=False, input_there=False, kind="failed", status="", waking=False)
    args.update(link)
    return peers_view.describe(peer or entry(), "Studio Mac", **args)


class DescribeTest(unittest.TestCase):
    def test_a_machine_not_in_use_shows_its_own_status(self):
        found = state(entry(in_use=False), up=True)
        self.assertEqual((found.tone, found.word), ("amber", "NOT IN USE"))

    def test_a_peer_whose_input_is_on_this_pc_is_driving_it(self):
        found = state(up=True, driving=True)
        self.assertEqual((found.word, found.tone), ("Driving this PC", "signal"))
        self.assertIn("Studio Mac", found.detail)

    def test_the_machine_holding_this_pcs_input_says_input_is_there(self):
        found = state(up=True, input_there=True)
        self.assertEqual((found.word, found.tone), ("Input here", "signal"))
        self.assertIn("Studio Mac", found.detail)

    def test_a_live_link_is_linked(self):
        self.assertEqual(state(up=True).word, "Linked")

    def test_a_link_still_looking_is_waiting_and_a_wake_is_named(self):
        self.assertEqual(state(kind="connected", status="Connecting").word, "Waiting")
        self.assertEqual(state().word, "Waiting")
        self.assertEqual(state(waking=True).detail, "Waking Studio Mac…")
        self.assertEqual(state(waking=True).word, "Waiting")

    def test_each_fault_kind_has_its_word_and_the_links_own_sentence(self):
        cases = {
            "unauthenticated": "Refused", "wrong_id": "Refused", "different": "Refused", "unreadable": "Refused",
            "older": "Version", "newer": "Version", "not_beamer": "Version",
            "unreachable": "Unreachable", "timeout": "Unreachable",
            "refused": "Not listening",
            "stopped": "Dropped", "closed": "Dropped", "failed": "Dropped",
        }
        for kind, word in cases.items():
            found = state(kind=kind, status=f"sentence for {kind}")
            self.assertEqual(found.word, word, kind)
            self.assertEqual(found.tone, "fault", kind)
            self.assertIn(f"sentence for {kind}", found.detail)

    def test_a_peer_that_closed_before_it_ever_linked_is_a_version_problem(self):
        found = state(entry(linked=False), kind="closed",
                      status="Update Beamer on Studio Mac to 1.5.0; if it is up to date, pair the two again")
        self.assertEqual(found.word, "Version")

    def test_any_1_4_pairing_is_asked_to_pair_again(self):
        # 1.4.x kept no record of whether its token was paired or typed, so a token of pairing's
        # shape is asked again too, and so is one an earlier beta linked.
        for peer in (entry(token="typed by hand"), entry(id="", linked=False, from_1_4=True), entry(from_1_4=True)):
            with self.subTest(peer=peer):
                found = state(peer, up=peer.get("from_1_4") is True)
                self.assertEqual((found.word, found.tone), ("Pair again", "amber"))
                self.assertEqual(found.detail, "Studio Mac was paired on Beamer 1.4. Pair the two again to link them on 1.5.0.")

    def test_sending_off_with_no_link_waits_for_the_other_end(self):
        found = state(entry(send=False))
        self.assertEqual(found.word, "Waiting")
        self.assertIn("connect", found.detail)

    def test_a_removed_or_stopped_link_is_not_a_fault(self):
        self.assertEqual(state(kind="removed").word, "Waiting")
        self.assertEqual(state(kind="off").word, "Waiting")

    def test_a_fault_with_no_sentence_still_says_something(self):
        self.assertTrue(state(kind="refused").detail)


class FactsTest(unittest.TestCase):
    def test_platform_names(self):
        for platform, name in (("macos", "Mac"), ("windows", "Windows"), ("linux", "Linux"), ("ios", "iPhone"),
                               ("android", "Android"), ("", "Other"), ("beos", "Other")):
            self.assertEqual(peers_view.platform_name({"platform": platform}), name)

    def test_the_address_line_is_the_address_and_port_and_hides_on_request(self):
        self.assertEqual(peers_view.address_line(entry(), False), "192.168.77.5 · port 24820")
        self.assertNotIn("192.168.77.5", peers_view.address_line(entry(), True))
        self.assertEqual(peers_view.address_line(entry(host="", port=0), False), "No address")
        self.assertEqual(peers_view.address_line(entry(host=""), False), "No address")


if __name__ == "__main__":
    unittest.main()
