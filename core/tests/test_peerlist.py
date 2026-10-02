"""What the windows do to the peers list, and the words they use for pairing, the same on every desktop."""

import copy
import time
import unittest

from core import pairing, peerlist

A = "AAAAAAAAAAAAAAAAAAAAAA"
B = "BBBBBBBBBBBBBBBBBBBBBw"
C = "CCCCCCCCCCCCCCCCCCCCCg"


def peer(ident, name="STUDIO-PC", token=None, **extra):
    return {"id": ident, "name": name, "platform": "windows", "token": token or ident * 3, "host": "10.1.1.2",
            "port": 24820, "send": True, "allow_drive": True, "side": "", "linked": True, "from_1_4": False,
            "paired_at": 1_700_000_000, **extra}


def settings(*peers, zones=()):
    return {"peers": [dict(entry) for entry in peers], "zones": [dict(zone) for zone in zones]}


class LabelTests(unittest.TestCase):
    def test_a_name_is_its_own_label_while_nothing_shares_it(self):
        labels = peerlist.labels([peer(A, "Desk"), peer(B, "Laptop")])
        self.assertEqual(labels, {A * 3: "Desk", B * 3: "Laptop"})

    def test_two_machines_with_one_name_each_show_the_last_four_characters_of_their_id(self):
        labels = peerlist.labels([peer(A, "Desk"), peer(B, "Desk"), peer(C, "Laptop")])
        self.assertEqual(labels[A * 3], "Desk (AAAA)")
        self.assertEqual(labels[B * 3], "Desk (BBBw)")
        self.assertEqual(labels[C * 3], "Laptop")

    def test_the_comparison_ignores_case_and_surrounding_space(self):
        labels = peerlist.labels([peer(A, "Desk"), peer(B, " desk ")])
        self.assertTrue(all("(" in label for label in labels.values()))

    def test_an_entry_with_no_id_yet_has_the_name_alone(self):
        labels = peerlist.labels([peer("", "Desk", token="legacy", from_1_4=True), peer(B, "Desk")])
        self.assertEqual(labels["legacy"], "Desk")
        self.assertEqual(labels[B * 3], "Desk (BBBw)")

    def test_an_entry_with_no_name_is_called_by_its_address_then_by_what_it_is(self):
        labels = peerlist.labels([peer(A, ""), peer(B, "", host="")])
        self.assertEqual(labels[A * 3], "10.1.1.2")
        self.assertEqual(labels[B * 3], "Unnamed machine")


class AddPeerTests(unittest.TestCase):
    def test_a_new_entry_goes_on_the_end(self):
        held = settings(peer(A))
        peerlist.add_peer(held, peer(B))
        self.assertEqual([entry["id"] for entry in held["peers"]], [A, B])

    def test_a_replaced_entry_gives_its_place_to_the_new_one(self):
        # The first entry is the one the flat settings read, so the new one must not slip behind.
        held = settings(peer("", "Old", token="legacy", from_1_4=True), peer(B))
        peerlist.add_peer(held, peer(A, "New"), replaced=copy.deepcopy(held["peers"][0]))
        self.assertEqual([entry["id"] for entry in held["peers"]], [A, B])

    def test_a_machine_paired_crosses_by_its_whole_edge_until_its_ways_are_chosen(self):
        # Pairing made no zones before beta.5, so a second machine's side, once set, led nowhere
        # (the rig and the laptop, 01-10-2026).
        held = settings(peer(A, side="left"), zones=[{"peer": A, "kind": "edge"}])
        peerlist.add_peer(held, peer(B, token="second"))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])

    def test_the_replaced_entrys_zones_go_with_it_when_it_was_matched_by_name_alone(self):
        held = settings(peer("", token="legacy", host="10.1.1.9", from_1_4=True), zones=[{"peer": "", "kind": "edge"}])
        peerlist.add_peer(held, peer(A), replaced=copy.deepcopy(held["peers"][0]))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"}])

    def test_the_machine_at_the_1_4_entrys_host_takes_over_its_side_and_zones(self):
        # Every 1.4.x upgrader pairs again (no token migrates), and the user is at the screen
        # pairing that machine on purpose: its arrangement is not theirs to set a second time.
        old = peer("", token="legacy", from_1_4=True, linked=False, side="left", side_set_at=1_790_000_000,
                   side_by=C, paired_at=0)
        held = settings(old, zones=[{"peer": "", "kind": "edge"}, {"peer": "", "kind": "corner", "corner": "top_left",
                                                                    "edge": "left", "off": True}])
        peerlist.add_peer(held, peer(A, token="fresh"), replaced=copy.deepcopy(old))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"},
                                         {"peer": A, "kind": "corner", "corner": "top_left", "edge": "left", "off": True}])
        entry = held["peers"][0]
        self.assertEqual((entry["side"], entry["side_set_at"], entry["side_by"]), ("left", 1_790_000_000, C))
        self.assertEqual(entry["token"], "fresh")

    def test_a_machine_at_that_host_of_another_platform_takes_nothing(self):
        old = peer("", token="legacy", platform="macos", from_1_4=True, side="left")
        held = settings(old, zones=[{"peer": "", "kind": "edge"}])
        peerlist.add_peer(held, peer(A, token="fresh"), replaced=copy.deepcopy(old))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"}])
        self.assertEqual(held["peers"][0]["side"], "")

    def test_an_entry_that_is_not_the_1_4_one_gives_nothing_by_host(self):
        old = peer(B, token="legacy", linked=False, side="left")
        held = settings(old, zones=[{"peer": B, "kind": "edge"}])
        peerlist.add_peer(held, peer(A, token="fresh"), replaced=copy.deepcopy(old))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"}])

    def test_zones_of_a_machine_paired_again_under_its_own_id_stay(self):
        held = settings(peer(A, linked=False), zones=[{"peer": A, "kind": "edge"}])
        peerlist.add_peer(held, peer(A, token="fresh"), replaced=copy.deepcopy(held["peers"][0]))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"}])
        self.assertEqual(held["peers"][0]["token"], "fresh")

    def test_other_machines_zones_are_left_alone(self):
        held = settings(peer(A), peer("", token="legacy", from_1_4=True), zones=[{"peer": A, "kind": "edge"}])
        peerlist.add_peer(held, peer(B), replaced=copy.deepcopy(held["peers"][1]))
        self.assertEqual(held["zones"], [{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])

    def test_an_entry_replaced_by_token_is_found_though_the_caller_holds_a_copy(self):
        held = settings(peer(A, token="one"))
        peerlist.add_peer(held, peer(B), replaced={**held["peers"][0]})
        self.assertEqual([entry["id"] for entry in held["peers"]], [B])


class RemovePeerTests(unittest.TestCase):
    def test_the_entry_and_its_zones_go(self):
        held = settings(peer(A), peer(B), zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])
        removed = peerlist.remove_peer(held, A * 3)
        self.assertEqual(removed["id"], A)
        self.assertEqual([entry["id"] for entry in held["peers"]], [B])
        self.assertEqual(held["zones"], [{"peer": B, "kind": "edge"}])

    def test_an_unknown_token_removes_nothing(self):
        held = settings(peer(A))
        self.assertIsNone(peerlist.remove_peer(held, "nope"))
        self.assertEqual(len(held["peers"]), 1)

    def test_the_entry_migrated_from_1_4_goes_with_the_zones_that_name_no_one(self):
        held = settings(peer("", token="legacy", from_1_4=True), zones=[{"peer": "", "kind": "edge"}])
        peerlist.remove_peer(held, "legacy")
        self.assertEqual((held["peers"], held["zones"]), ([], []))


class WordsTests(unittest.TestCase):
    def test_every_requester_error_has_a_sentence_naming_the_machine(self):
        for reason in (pairing.ERROR_NOT_PAIRING, pairing.ERROR_REFUSED, pairing.ERROR_VERSION, "no_answer"):
            text = peerlist.pairing_error_text(pairing.PairingError(reason), "Desk")
            self.assertIn("Desk", text, reason)
            self.assertTrue(text.endswith("."), reason)

    def test_an_old_beamer_is_told_to_update_to_1_5_0(self):
        text = peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_VERSION), "Desk")
        self.assertEqual(text, "Desk runs an older Beamer. Update Beamer on it to 1.5.0, then pair again.")

    def test_known_names_the_entry_and_the_day_it_was_paired(self):
        entry = peer(A, "Desk", paired_at=int(time.mktime((2026, 9, 24, 12, 0, 0, 0, 0, -1))))
        text = peerlist.pairing_error_text(pairing.AlreadyPaired(entry), "that machine")
        self.assertIn("Desk", text)
        self.assertIn("24-09-2026", text)
        self.assertIn("Remove", text)

    def test_known_without_a_day_for_an_entry_that_has_none(self):
        text = peerlist.pairing_error_text(pairing.AlreadyPaired(peer(A, "Desk", paired_at=0)), "that machine")
        self.assertNotIn("1970", text)
        self.assertIn("Desk", text)

    def test_known_where_the_other_machine_claimed_this_ones_own_id(self):
        text = peerlist.pairing_error_text(pairing.AlreadyPaired(None), "that machine")
        self.assertIn("this machine", text.lower())

    def test_full_may_be_either_machine_and_known_from_the_host_says_to_remove_this_one_there(self):
        full = peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_FULL), "Desk")
        self.assertIn("Either this machine or Desk", full)
        self.assertIn("Remove one", full)
        known = peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_KNOWN), "Desk")
        self.assertEqual(known, "Desk already has this machine paired. Remove this machine from its list there, then pair again.")

    def test_not_saved_and_busy_say_what_to_do(self):
        self.assertIn("could not save", peerlist.pairing_error_text(pairing.PairingError("not_saved"), "Desk"))
        self.assertIn("moment", peerlist.pairing_error_text(pairing.PairingError("busy"), "Desk"))

    def test_a_reason_nobody_has_seen_is_still_said(self):
        self.assertEqual(peerlist.pairing_error_text(pairing.PairingError("strange"), "Desk"), "Pairing failed: strange.")

    def test_every_outcome_a_code_can_end_in_has_a_sentence(self):
        for outcome in ("paired", "refused", "version", "expired", "known", "known_there", "full", "not_saved"):
            text = peerlist.host_outcome_text(outcome, peer(A, "Desk"))
            self.assertTrue(text and text.endswith("."), outcome)

    def test_known_there_says_what_wire_md_says(self):
        self.assertIn("That machine may already be paired here, or the code was wrong.",
                      peerlist.host_outcome_text("known_there", None))

    def test_a_code_that_ended_without_an_outcome_says_nothing(self):
        self.assertEqual(peerlist.host_outcome_text(None, None), "")


class LabelForTests(unittest.TestCase):
    def test_finds_the_label_by_token_by_id_or_by_host(self):
        first, second = peer(A, "Studio"), peer(B, "studio")
        first.update(host="192.0.2.10")
        second.update(host="192.0.2.11")
        labels = peerlist.labels([first, second])
        self.assertEqual(peerlist.label_for([first, second], token=second["token"]), labels[second["token"]])
        self.assertEqual(peerlist.label_for([first, second], peer_id=first["id"]), labels[first["token"]])
        self.assertEqual(peerlist.label_for([first, second], host="192.0.2.11"), labels[second["token"]])
        self.assertIn("(", peerlist.label_for([first, second], token=first["token"]))

    def test_a_machine_nobody_knows_has_no_label(self):
        self.assertEqual(peerlist.label_for([peer(A, "Desk")], token="nope"), "")
        self.assertEqual(peerlist.label_for([], peer_id="nope"), "")


class SharingANameTests(unittest.TestCase):
    def test_names_are_compared_as_labels_compares_them(self):
        machines = [{"name": " Studio "}, {"name": "studio"}, {"name": "Other"}]
        self.assertEqual(peerlist.sharing_a_name(machines), [True, True, False])

    def test_a_lone_machine_shares_with_nobody(self):
        self.assertEqual(peerlist.sharing_a_name([{"name": "Studio"}]), [False])
        self.assertEqual(peerlist.sharing_a_name([]), [])


if __name__ == "__main__":
    unittest.main()
