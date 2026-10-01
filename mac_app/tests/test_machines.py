"""The Overview's list of machines: what each row says, decided without AppKit."""

import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import machines

A = "AAAAAAAAAAAAAAAAAAAAAA"
TOKEN = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def entry(**extra):
    return {"id": A, "name": "STUDIO-PC", "platform": "windows", "token": TOKEN, "host": "10.1.1.2", "port": 24820,
            "send": True, "allow_drive": True, "linked": True, **extra}


def row(**kwargs):
    values = dict(entry=entry(), label="STUDIO-PC", live=False, kind="connected", status="", here=False, driving=False,
                  inbound=False, has_link=True)
    values.update(kwargs)
    return machines.row(**values)


class StateTests(unittest.TestCase):
    def test_a_machine_driving_this_mac_beats_the_rest(self):
        state = row(driving=True, live=True, here=True).state
        self.assertEqual((state.key, state.tone, state.word), ("driving", "signal", "Driving this Mac"))

    def test_input_here_beats_a_plain_link(self):
        state = row(live=True, here=True).state
        self.assertEqual((state.key, state.tone), ("here", "signal"))

    def test_a_live_link_says_linked(self):
        self.assertEqual(row(live=True).state.key, "linked")

    def test_the_details_are_the_ones_windows_uses(self):
        self.assertEqual(row(driving=True).state.detail,
                         "STUDIO-PC's keyboard and pointer are on this Mac. Push the pointer back through the edge it arrived by to send them home.")
        self.assertEqual(row(here=True).state.detail,
                         "This Mac's keyboard and pointer are on STUDIO-PC. Do the same again to bring them home.")
        self.assertEqual(row(live=True).state.detail, "Connected to STUDIO-PC.")
        self.assertEqual(row(kind="none").state.detail, "Looking for STUDIO-PC. It connects on its own once Beamer is open there.")
        self.assertEqual(row(has_link=False, entry=entry(send=False)).state.detail, "Waiting for STUDIO-PC to connect.")

    def test_a_typed_1_4_token_is_asked_to_be_paired_again(self):
        state = row(entry=entry(token="typed-in-1.4", id="", from_1_4=True), kind="failed").state
        self.assertEqual((state.key, state.tone, state.word), ("pair_again", "amber", "Pair again"))
        self.assertEqual(state.detail, "STUDIO-PC was paired in 1.4.x with a typed token, which 1.5.0 does not use. Remove it and pair it again.")

    def test_a_machine_linked_in_but_not_dialled_is_linked_too(self):
        state = row(has_link=False, inbound=True, entry=entry(send=False)).state
        self.assertEqual(state.key, "linked")

    def test_a_machine_this_mac_does_not_drive_and_nothing_is_linked_waits_for_it(self):
        state = row(has_link=False, entry=entry(send=False)).state
        self.assertEqual((state.key, state.tone), ("waiting", "amber"))
        self.assertIn("STUDIO-PC", state.detail)

    def test_faults_say_what_the_link_said(self):
        for kind, key in (("unauthenticated", "token"), ("wrong_id", "token"), ("different", "token"),
                          ("unreadable", "token"), ("older", "version"), ("newer", "version"), ("not_beamer", "version"),
                          ("unreachable", "unreachable"), ("timeout", "unreachable"), ("refused", "not_listening"),
                          ("blocked", "blocked"), ("stopped", "dropped")):
            state = row(kind=kind, status=f"said {kind}").state
            self.assertEqual((state.key, state.tone), (key, "fault"), kind)
            self.assertEqual(state.detail, f"said {kind}" if key != "dropped" else "said stopped. Beamer keeps trying to reconnect.", kind)

    def test_a_v5_machine_that_closed_on_a_first_link_is_a_version_fault_in_the_links_words(self):
        said = "Update Beamer on STUDIO-PC to 1.5.0; if it is up to date, pair the two again"
        state = row(entry=entry(linked=False), kind="closed", status=said).state
        self.assertEqual((state.key, state.tone, state.detail), ("version", "fault", said))

    def test_a_machine_that_closed_after_linking_once_is_only_dropped(self):
        state = row(kind="closed", status="STUDIO-PC closed the connection: it may have removed this machine").state
        self.assertEqual((state.key, state.tone), ("dropped", "fault"))
        self.assertTrue(state.detail.endswith(". Beamer keeps trying to reconnect."))

    def test_a_link_that_failed_in_some_way_is_dropped_and_one_with_nothing_to_say_is_waiting(self):
        self.assertEqual(row(kind="failed", status="The link to STUDIO-PC failed: broken pipe").state.key, "dropped")
        self.assertEqual(row(kind="failed", status="").state.key, "waiting")

    def test_a_link_still_looking_is_waiting_not_a_fault(self):
        for kind in ("none", "off", "connected", "removed"):
            state = row(kind=kind, status="Connecting to 10.1.1.2:24820").state
            self.assertEqual((state.key, state.tone), ("waiting", "amber"), kind)
            self.assertIn("STUDIO-PC", state.detail)


class RowTests(unittest.TestCase):
    def test_the_row_names_the_machine_and_what_it_is(self):
        shown = row(label="STUDIO-PC (AAAA)")
        self.assertEqual((shown.label, shown.platform, shown.token), ("STUDIO-PC (AAAA)", "Windows", TOKEN))

    def test_the_address_is_the_saved_one_with_its_port(self):
        self.assertEqual(row().address, "10.1.1.2 · port 24820")

    def test_a_machine_with_no_address_says_so(self):
        self.assertEqual(row(entry=entry(host="", port=0)).address, "No address")

    def test_each_direction_is_the_entrys_own_switch(self):
        shown = row(entry=entry(send=False, allow_drive=True))
        self.assertEqual((shown.drives, shown.driven), (False, True))

    def test_platforms_have_names_and_an_unknown_one_is_shown_as_it_came(self):
        self.assertEqual(machines.platform_name("macos"), "Mac")
        self.assertEqual(machines.platform_name("linux"), "Linux")
        self.assertEqual(machines.platform_name("ios"), "iPhone")
        self.assertEqual(machines.platform_name("plan9"), "Other")
        self.assertEqual(machines.platform_name(""), "Other")


if __name__ == "__main__":
    unittest.main()
