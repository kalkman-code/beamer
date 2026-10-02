"""core/ways.py: a machine's zones read and written one peer at a time, the side settled with each
peer, the machine the shortcut picks, and the pairings a zone needs (WIRE.md sections 5 and 8)."""

import unittest

from core import protocol, ways

HERE = protocol.id_text(bytes(range(1, 17)))
A = protocol.id_text(bytes(range(20, 36)))
B = protocol.id_text(bytes(range(40, 56)))
C = protocol.id_text(bytes(range(60, 76)))
PHONE = protocol.id_text(bytes(range(80, 96)))


def peer(ident, name, **fields):
    data = {"id": ident, "name": name, "platform": "windows", "host": "10.9.9.9", "port": 24820, "hw": "",
            "send": True, "allow_drive": True, "side": "", "side_set_at": 0, "side_by": "", "paired_with": [],
            "paired_at": 1, "linked": True, "from_1_4": False, "token": "k" + ident}
    data.update(fields)
    return data


def settings(*peers, zones=()):
    return {"machine_id": HERE, "shortcut": True, "peers": list(peers), "zones": list(zones)}


def mac_corner_edge(corner, side):
    return corner.split("_")[1]


def pc_corner_edge(corner, side):
    return side if side in ways.EDGES else corner.split("_")[1]


MAC = ("edge", "part", "corner", "notch")
PC = ("edge", "part", "corner")


class ReadingOnePeer(unittest.TestCase):
    def test_a_peer_with_no_zones_reads_as_nothing_in_use_and_the_defaults(self):
        found = ways.ways(settings(peer(A, "Ay", side="left")), A)
        self.assertEqual(found, {"side": "left", "methods": [], "parts": ["middle"], "corner": "top_left"})

    def test_only_that_peers_zones_in_use_count(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="right"), zones=[
            {"peer": A, "kind": "edge"},
            {"peer": A, "kind": "corner", "corner": "bottom_left", "edge": "left", "off": True},
            {"peer": B, "kind": "part", "parts": ["start", "end"]},
            {"peer": B, "kind": "corner", "corner": "top_right", "edge": "right"},
        ])
        self.assertEqual(ways.ways(held, A), {"side": "left", "methods": ["edge"], "parts": ["middle"], "corner": "bottom_left"})
        self.assertEqual(ways.ways(held, B), {"side": "right", "methods": ["part", "corner"], "parts": ["start", "end"],
                                              "corner": "top_right"})

    def test_an_unknown_peer_reads_as_nothing(self):
        self.assertEqual(ways.ways(settings(), A)["methods"], [])


class WritingOnePeer(unittest.TestCase):
    def test_one_zone_of_each_kind_is_written_for_that_peer_and_the_others_are_left_alone(self):
        other = {"peer": B, "kind": "edge"}
        held = settings(peer(A, "Ay"), peer(B, "Bee", side="right"), zones=[other])
        changed = ways.edit(held, A, side="left", methods=["corner"], parts=["start"], corner="top_left",
                            kinds=MAC, corner_edge=mac_corner_edge, now=1000)
        self.assertTrue(changed)
        mine = [zone for zone in held["zones"] if zone["peer"] == A]
        self.assertEqual(sorted(zone["kind"] for zone in mine), sorted(MAC))
        self.assertEqual({zone["kind"] for zone in mine if not zone.get("off")}, {"corner"})
        self.assertEqual(next(zone for zone in mine if zone["kind"] == "corner")["edge"], "left")
        self.assertIn(other, held["zones"])
        self.assertEqual(held["peers"][1]["side"], "right")

    def test_a_new_side_is_stamped_by_this_machine_and_never_below_the_last_stamp(self):
        held = settings(peer(A, "Ay", side="right", side_set_at=5000, side_by=A))
        self.assertTrue(ways.edit(held, A, side="top", methods=["edge"], parts=["middle"], corner="top_left",
                                  kinds=PC, corner_edge=pc_corner_edge, now=100))
        entry = held["peers"][0]
        self.assertEqual((entry["side"], entry["side_set_at"], entry["side_by"]), ("top", 5001, HERE))

    def test_an_unchanged_side_keeps_its_stamp_and_says_so(self):
        held = settings(peer(A, "Ay", side="right", side_set_at=5000, side_by=A))
        self.assertFalse(ways.edit(held, A, side="right", methods=["part"], parts=["end"], corner="top_left",
                                   kinds=PC, corner_edge=pc_corner_edge, now=9000))
        self.assertEqual((held["peers"][0]["side_set_at"], held["peers"][0]["side_by"]), (5000, A))

    def test_the_pc_corner_crosses_the_side(self):
        held = settings(peer(A, "Ay"))
        ways.edit(held, A, side="top", methods=["corner"], parts=["middle"], corner="top_left",
                  kinds=PC, corner_edge=pc_corner_edge, now=1)
        corner = next(zone for zone in held["zones"] if zone["kind"] == "corner")
        self.assertEqual(corner["edge"], "top")

    def test_edge_and_its_thirds_both_on_write_the_thirds_off(self):
        held = settings(peer(A, "Ay"))
        ways.edit(held, A, side="left", methods=["edge", "part"], parts=["start"], corner="top_left",
                  kinds=PC, corner_edge=pc_corner_edge, now=1)
        self.assertEqual(ways.ways(held, A)["methods"], ["edge"])

    def test_an_unknown_peer_raises(self):
        with self.assertRaises(KeyError):
            ways.edit(settings(), A, side="left", methods=[], parts=["middle"], corner="top_left",
                      kinds=PC, corner_edge=pc_corner_edge, now=1)

    def test_a_zone_the_user_kept_keeps_its_other_fields(self):
        held = settings(peer(A, "Ay", side="left"), zones=[{"peer": A, "kind": "edge", "off": True, "later": 1}])
        ways.edit(held, A, side="left", methods=["edge"], parts=["middle"], corner="top_left",
                  kinds=PC, corner_edge=pc_corner_edge, now=1)
        self.assertEqual([zone for zone in held["zones"] if zone["kind"] == "edge"], [{"peer": A, "kind": "edge", "later": 1}])


class Clashes(unittest.TestCase):
    def test_two_machines_on_one_whole_edge_clash_and_the_sentence_names_both(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])
        self.assertEqual(ways.clash(held, "this Mac"),
                         "Ay and Bee would both lead from the right edge of this Mac. Move one of them to another "
                         "side, or give each its own thirds of the edge.")

    def test_two_machines_on_different_thirds_of_one_edge_do_not_clash(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"), zones=[
            {"peer": A, "kind": "part", "parts": ["start"]}, {"peer": B, "kind": "part", "parts": ["middle", "end"]}])
        self.assertIsNone(ways.clash(held, "this PC"))

    def test_a_corner_inside_another_machines_edge_does_not_clash(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="top"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": B, "kind": "corner", "corner": "top_right", "edge": "top"}])
        self.assertIsNone(ways.clash(held, "this PC"))

    def test_one_corner_for_two_machines_clashes(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="top"), zones=[
            {"peer": A, "kind": "corner", "corner": "top_right", "edge": "right"},
            {"peer": B, "kind": "corner", "corner": "top_right", "edge": "top"}])
        self.assertEqual(ways.clash(held, "this PC"),
                         "Ay and Bee would both lead from the top right corner of this PC. Choose another corner "
                         "for one of them.")

    def test_one_notch_for_two_machines_clashes(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="top"), zones=[
            {"peer": A, "kind": "notch"}, {"peer": B, "kind": "notch"}])
        self.assertEqual(ways.clash(held, "this Mac"),
                         "Ay and Bee would both lead from the notch of this Mac. Turn the notch off for one of them.")

    def test_two_of_one_machines_own_zones_over_one_stretch_clash(self):
        held = settings(peer(A, "Ay", side="right"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": A, "kind": "part", "parts": ["middle"]}])
        self.assertEqual(ways.clash(held, "this Mac"), "Two of the ways to Ay cover the right edge of this Mac. Keep one of them.")

    def test_zones_that_are_off_never_clash(self):
        held = settings(peer(A, "Ay", side="right"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        self.assertIsNone(ways.clash(held, "this PC"))


class EveryMachineHasAWayIn(unittest.TestCase):
    """A machine paired, or one whose side arrives, crosses by its whole edge unless that edge is
    another machine's already (the rig's beta.3 file of 01-10: the laptop's side set, no zone)."""

    def test_a_machine_with_no_zone_gets_its_whole_edge(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="right"), zones=[{"peer": A, "kind": "edge"}])
        self.assertTrue(ways.ensure_zones(held))
        self.assertIn({"peer": B, "kind": "edge"}, held["zones"])
        self.assertFalse(ways.ensure_zones(held))

    def test_one_whose_side_is_another_machines_edge_gets_it_off_so_the_settings_stay_readable(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="left"), zones=[{"peer": A, "kind": "edge"}])
        ways.ensure_zones(held)
        self.assertIn({"peer": B, "kind": "edge", "off": True}, held["zones"])
        self.assertIsNone(ways.clash(held, "this PC"))

    def test_a_machine_with_its_zones_all_off_keeps_them_as_they_are(self):
        held = settings(peer(A, "Ay", side="left"), zones=[{"peer": A, "kind": "edge", "off": True}])
        self.assertFalse(ways.ensure_zones(held))

    def test_a_phone_and_an_entry_with_no_id_get_none(self):
        held = settings(peer(PHONE, "Phone", port=0, host="", send=False), peer("", "Old", from_1_4=True))
        self.assertFalse(ways.ensure_zones(held))
        self.assertEqual(held["zones"], [])

    def test_an_arrangement_for_a_machine_with_no_zone_gives_it_its_edge(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee"), zones=[{"peer": A, "kind": "edge"}])
        self.assertEqual(ways.arrangement(held, B, "left", 20, B, "this PC"), (True, []))
        self.assertIn({"peer": B, "kind": "edge"}, held["zones"])

    def test_an_arrangement_onto_another_machines_edge_gives_it_the_edge_off_and_says_so(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop"), zones=[{"peer": A, "kind": "edge"}])
        changed, notices = ways.arrangement(held, B, "right", 20, B, "this PC")
        self.assertTrue(changed)
        self.assertEqual(notices, ["MacBook Pro and Laptop now lead from the same part of this PC's screen, so "
                                   "Laptop's edge is off."])
        self.assertIn({"peer": B, "kind": "edge", "off": True}, held["zones"])


class Blocked(unittest.TestCase):
    def test_a_machine_whose_side_is_taken_and_has_no_way_in_is_told_which_machine_has_it(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop", side="left"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"),
                         "Laptop has no way in: MacBook Pro already leads from the left edge of this PC. "
                         "Give each its own thirds of that edge under Part of the edge, or move one to another side.")
        self.assertEqual(ways.blocked_sentence(held, A, "this PC"), "")

    def test_thirds_left_free_by_another_machine_are_no_block(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop", side="left"), zones=[
            {"peer": A, "kind": "part", "parts": ["start"]}, {"peer": B, "kind": "part", "parts": ["end"]}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"), "")

    def test_a_machine_with_a_corner_in_use_is_not_blocked(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop", side="left"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge", "off": True},
            {"peer": B, "kind": "corner", "corner": "top_left", "edge": "left"}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"), "")

    def test_a_machine_with_no_side_is_not_blocked(self):
        held = settings(peer(A, "MacBook Pro", side="left"), peer(B, "Laptop"), zones=[{"peer": A, "kind": "edge"}])
        self.assertEqual(ways.blocked_sentence(held, B, "this PC"), "")


class WayBack(unittest.TestCase):
    """What a machine tells another with its `arrangement`: whether it has a zone in use leading there."""

    def test_a_zone_in_use_with_a_side_or_a_corner_is_a_way(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee"), peer(C, "Sea", side="top"), zones=[
            {"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"},
            {"peer": C, "kind": "edge", "off": True}, {"peer": C, "kind": "corner", "corner": "top_left", "edge": "top"}])
        self.assertEqual([ways.has_way(held, ident) for ident in (A, B, C)], [True, False, True])

    def test_an_arrangement_keeps_what_the_sender_said_whether_or_not_its_side_is_newer(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=50, side_by=A))
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False), (True, []))
        self.assertIs(held["peers"][0]["way_back"], False)
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC", way_back=False), (False, []))
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC"), (False, []))
        self.assertIs(held["peers"][0]["way_back"], False)
        self.assertEqual(ways.arrangement(held, A, "right", 50, A, "this PC", way_back=True), (True, []))
        self.assertIs(held["peers"][0]["way_back"], True)

    def test_the_sentence_names_the_machine_with_no_way_back(self):
        held = settings(peer(A, "Desk-PC", way_back=False), peer(B, "Bee", way_back=True), peer(C, "Sea"))
        self.assertEqual(ways.no_way_back_sentence(held, A),
                         "Desk-PC has no way back to this machine: none of its edges leads here. Its own "
                         "Crossing page says why.")
        self.assertEqual(ways.no_way_back_sentence(held, B), "")
        self.assertEqual(ways.no_way_back_sentence(held, C), "")


class Arrangement(unittest.TestCase):
    def test_a_newer_arrangement_sets_the_opposite_side_for_that_peer_only(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=10, side_by=HERE), peer(B, "Bee", side="right"))
        changed, notices = ways.arrangement(held, A, "left", 20, A, "this PC")
        self.assertTrue(changed)
        self.assertEqual(notices, [])
        self.assertEqual((held["peers"][0]["side"], held["peers"][0]["side_set_at"], held["peers"][0]["side_by"]),
                         ("right", 20, A))
        self.assertEqual(held["peers"][1]["side"], "right")

    def test_an_older_one_or_a_tie_lost_on_the_id_changes_nothing(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=10, side_by=C))
        self.assertEqual(ways.arrangement(held, A, "left", 9, A, "this PC"), (False, []))
        self.assertEqual(ways.arrangement(held, A, "left", 10, A, "this PC"), (False, []))
        self.assertEqual(held["peers"][0]["side"], "left")

    def test_a_tie_won_on_the_id_applies(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=10, side_by=A))
        self.assertEqual(ways.arrangement(held, A, "left", 10, C, "this PC"), (True, []))

    def test_a_side_that_now_overlaps_turns_the_senders_zones_off_and_names_both(self):
        held = settings(peer(A, "Ay", side="left"), peer(B, "Bee", side="right"),
                        zones=[{"peer": A, "kind": "edge"}, {"peer": B, "kind": "edge"}])
        changed, notices = ways.arrangement(held, A, "left", 20, A, "this Mac")
        self.assertTrue(changed)
        self.assertEqual(notices, ["Bee and Ay now lead from the same part of this Mac's screen, so Ay's edge is off."])
        self.assertTrue(next(zone for zone in held["zones"] if zone["peer"] == A)["off"])
        self.assertNotIn("off", next(zone for zone in held["zones"] if zone["peer"] == B))

    def test_an_unknown_peer_bad_edge_or_bad_by_changes_nothing(self):
        held = settings(peer(A, "Ay", side="left"))
        self.assertEqual(ways.arrangement(held, B, "left", 20, B, "this PC"), (False, []))
        self.assertEqual(ways.arrangement(held, A, "middle", 20, A, "this PC"), (False, []))
        self.assertEqual(ways.arrangement(held, A, "left", 20, "nope", "this PC"), (False, []))


class TheShortcutsMachine(unittest.TestCase):
    def test_the_machine_input_was_last_on_while_it_can_take_input(self):
        peers = [peer(A, "Ay"), peer(B, "Bee")]
        self.assertEqual(ways.shortcut_peer(peers, B, lambda ident: True), B)

    def test_else_the_first_that_can_take_input(self):
        peers = [peer(A, "Ay"), peer(B, "Bee"), peer(C, "Sea")]
        self.assertEqual(ways.shortcut_peer(peers, A, lambda ident: ident == C), C)
        self.assertEqual(ways.shortcut_peer(peers, None, lambda ident: ident != A), B)

    def test_else_the_first_this_machine_sends_to_so_the_refusal_or_the_wake_names_it(self):
        peers = [peer(A, "Ay", send=False), peer(B, "Bee"), peer(C, "Sea")]
        self.assertEqual(ways.shortcut_peer(peers, None, lambda ident: False), B)

    def test_a_machine_this_one_does_not_send_to_is_never_picked(self):
        peers = [peer(A, "Ay", send=False)]
        self.assertIsNone(ways.shortcut_peer(peers, A, lambda ident: True))

    def test_a_phone_or_an_entry_without_an_id_is_never_picked(self):
        peers = [peer(PHONE, "Phone", port=0, host="", send=False), peer("", "Old", from_1_4=True)]
        self.assertIsNone(ways.shortcut_peer(peers, None, lambda ident: True))

    def test_nothing_paired_picks_nothing(self):
        self.assertIsNone(ways.shortcut_peer([], None, lambda ident: True))


class MissingPairings(unittest.TestCase):
    def test_a_machine_that_may_drive_this_one_and_is_not_paired_with_the_zones_peer_is_named(self):
        held = settings(peer(A, "Ay", paired_with=[B]), peer(B, "Bee", paired_with=[A]), peer(C, "Sea", paired_with=[A]))
        self.assertEqual([entry["id"] for entry in ways.missing_pairings(held, B)], [C])
        self.assertEqual([entry["id"] for entry in ways.missing_pairings(held, A)], [])

    def test_a_machine_not_allowed_to_drive_this_one_needs_no_pairing(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee", allow_drive=False))
        self.assertEqual(ways.missing_pairings(held, A), [])

    def test_a_machine_that_has_never_linked_is_not_judged(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee", linked=False))
        self.assertEqual(ways.missing_pairings(held, A), [])

    def test_a_phone_that_may_drive_this_one_counts(self):
        held = settings(peer(A, "Ay"), peer(PHONE, "Phone", port=0, host="", send=False))
        self.assertEqual([entry["id"] for entry in ways.missing_pairings(held, A)], [PHONE])

    def test_the_sentence_names_each_machine_and_the_one_it_needs(self):
        held = settings(peer(A, "Ay"), peer(B, "Bee", paired_with=[]))
        self.assertEqual(ways.missing_sentence(held, A, "this Mac"),
                         "While Bee drives this Mac, this way to Ay does nothing: Bee is not paired with Ay. "
                         "Pair them to use it from there too.")
        held["peers"].append(peer(C, "Sea"))
        self.assertEqual(ways.missing_sentence(held, A, "this Mac"),
                         "While Bee or Sea drives this Mac, this way to Ay does nothing: neither is paired "
                         "with Ay. Pair them to use it from there too.")
        self.assertEqual(ways.missing_sentence(settings(peer(A, "Ay")), A, "this Mac"), "")



class TheShownDefault(unittest.TestCase):
    """The Mac's page shows Right for its one machine before a side is set, and a save writes it."""

    def test_a_side_written_as_the_pages_default_is_unstamped_so_the_peers_own_wins(self):
        held = settings(peer(A, "Ay"), zones=[{"peer": A, "kind": "edge"}])
        self.assertTrue(ways.edit(held, A, side="right", methods=["edge"], parts=["middle"], corner="top_left",
                                  kinds=MAC, corner_edge=mac_corner_edge, now=1_900_000_000, default=True))
        entry = held["peers"][0]
        self.assertEqual((entry["side"], entry["side_set_at"], entry["side_by"]), ("right", 0, ""))
        self.assertEqual(ways.arrangement(held, A, "bottom", 1_899_999_000, A, "this Mac"), (True, []))
        self.assertEqual(held["peers"][0]["side"], "top")

    def test_a_default_never_moves_a_side_already_set(self):
        held = settings(peer(A, "Ay", side="left", side_set_at=5, side_by=A), zones=[{"peer": A, "kind": "edge"}])
        self.assertFalse(ways.edit(held, A, side="right", methods=["edge"], parts=["middle"], corner="top_left",
                                   kinds=MAC, corner_edge=mac_corner_edge, now=1_900_000_000, default=True))
        self.assertEqual(held["peers"][0]["side"], "left")


class TheCornerFollowsAnArrivingSide(unittest.TestCase):
    def test_a_corner_that_crosses_the_side_is_rewritten_for_the_side_that_arrives(self):
        held = settings(peer(A, "Ay", side="top", side_set_at=5, side_by=A),
                        zones=[{"peer": A, "kind": "corner", "corner": "top_right", "edge": "top"}])
        self.assertEqual(ways.arrangement(held, A, "left", 10, A, "this PC", corner_edge=pc_corner_edge), (True, []))
        self.assertEqual((held["peers"][0]["side"], held["zones"][0]["edge"]), ("right", "right"))


if __name__ == "__main__":
    unittest.main()
