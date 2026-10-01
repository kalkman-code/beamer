"""Firewall interpretation and the advice it turns into, exercised on any platform.

firewall_win's PowerShell stays untouched here: every test hands interpret() the JSON shape
the status script emits, so the decisions run on the Mac with no Windows at all.
"""

import unittest
import os
import sys
from unittest import mock

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import firewall_win
from firewall_win import advise, interpret

EXE = r"C:\Users\alex\AppData\Local\Beamer\Beamer.exe"
PORT = 51820


def rule(action="Allow", profile="Private", program=EXE, port="51820", protocol="TCP", enabled="True", direction="Inbound"):
    return {
        "name": "{x}", "display": "Beamer Receiver (TCP-In)", "enabled": enabled, "action": action,
        "direction": direction, "profile": profile, "program": program, "protocol": protocol,
        "localPort": [port] if port is not None else ["Any"],
    }


def pairing_rule(action="Allow", profile="Private", program=EXE, enabled="True"):
    """The UDP beacon rule repair() and the installer write beside the TCP one."""
    return {
        "name": "{y}", "display": "Beamer Pairing (UDP-In)", "enabled": enabled, "action": action,
        "direction": "Inbound", "profile": profile, "program": program, "protocol": "UDP",
        "localPort": [str(firewall_win.PAIRING_PORT)],
    }


def pairing_tcp_rule(action="Allow", profile="Private", program=EXE, enabled="True"):
    """The TCP rule for the pairing exchange, hosted while a pairing code is on screen."""
    return {
        "name": "{z}", "display": "Beamer Pairing (TCP-In)", "enabled": enabled, "action": action,
        "direction": "Inbound", "profile": profile, "program": program, "protocol": "TCP",
        "localPort": [str(firewall_win.PAIRING_PORT)],
    }


def network(category="Private", alias="Wi-Fi", index=5, ipv4="Internet", ipv6="NoTraffic"):
    return {"alias": alias, "index": index, "category": category, "ipv4": ipv4, "ipv6": ipv6}


def payload(rules=(), networks=(network(),)):
    return {"rules": list(rules), "networks": list(networks)}


class InterpretTests(unittest.TestCase):
    def test_no_rule_at_all(self):
        status = interpret(payload(), EXE, PORT)
        self.assertFalse(status.allowed)
        self.assertFalse(status.blocked)
        self.assertFalse(status.covered)
        self.assertEqual(status.active_profiles, ("Private",))
        self.assertEqual(advise(status).action, "repair")

    def test_correct_rule_on_private_lan(self):
        status = interpret(payload([rule(), pairing_rule(), pairing_tcp_rule()]), EXE, PORT)
        self.assertTrue(status.allowed)
        self.assertTrue(status.covered)
        self.assertTrue(status.pairing_allowed)
        self.assertFalse(status.blocked)
        self.assertEqual(status.public_interfaces, ())
        advice = advise(status)
        self.assertEqual(advice.action, "check")
        self.assertIn("lets your Mac reach Beamer", advice.sentence)

    def test_rule_scoped_to_a_profile_the_lan_is_not_on(self):
        # The installer's Private-only rule, but the LAN has been reclassified Public.
        status = interpret(payload([rule()], [network("Public", alias="Ethernet", index=9)]), EXE, PORT)
        self.assertTrue(status.allowed)
        self.assertFalse(status.covered)
        self.assertEqual(status.public_interfaces, ((9, "Ethernet"),))
        advice = advise(status)
        self.assertEqual(advice.action, "trust")
        self.assertIn("Ethernet", advice.sentence)
        self.assertEqual(advice.button, "Trust this network")

    def test_public_only_rule_on_private_lan_is_rewritten_not_trusted(self):
        status = interpret(payload([rule(profile="Public")]), EXE, PORT)
        self.assertTrue(status.allowed)
        self.assertFalse(status.covered)
        self.assertEqual(status.public_interfaces, ())
        self.assertEqual(advise(status).action, "repair")

    def test_block_rule_beats_allow_rule(self):
        # What Windows writes when Cancel is pressed on its own prompt: a block, any port, per profile.
        rules = [rule(), rule(action="Block", profile="Private, Public", port="Any"), rule(action="Block", profile="Private, Public", port="Any", protocol="UDP")]
        status = interpret(payload(rules), EXE, PORT)
        self.assertTrue(status.allowed)
        self.assertTrue(status.blocked)
        advice = advise(status)
        self.assertEqual(advice.action, "repair")
        self.assertIn("blocking", advice.sentence)

    def test_block_rule_on_an_inactive_profile_is_harmless(self):
        status = interpret(payload([rule(), pairing_rule(), pairing_tcp_rule(), rule(action="Block", profile="Public", port="Any")]), EXE, PORT)
        self.assertFalse(status.blocked)
        self.assertEqual(advise(status).action, "check")

    def test_disabled_block_rule_is_ignored(self):
        status = interpret(payload([rule(), rule(action="Block", port="Any", enabled="False")]), EXE, PORT)
        self.assertFalse(status.blocked)

    def test_executable_path_not_matching_the_rule(self):
        status = interpret(payload([rule(program=r"C:\Users\alex\code\Beamer\dist\Beamer.exe")]), EXE, PORT)
        self.assertFalse(status.allowed)
        self.assertEqual(advise(status).action, "repair")

    def test_path_match_ignores_case_and_slashes(self):
        status = interpret(payload([rule(program=EXE.lower().replace("\\", "/"))]), EXE, PORT)
        self.assertTrue(status.allowed)

    def test_port_must_cover_the_listen_port(self):
        self.assertFalse(interpret(payload([rule(port="51821")]), EXE, PORT).allowed)
        self.assertTrue(interpret(payload([rule(port="51000-52000")]), EXE, PORT).allowed)
        self.assertTrue(interpret(payload([rule(port="Any")]), EXE, PORT).allowed)
        self.assertFalse(interpret(payload([rule(protocol="UDP")]), EXE, PORT).allowed)
        self.assertFalse(interpret(payload([rule(direction="Outbound")]), EXE, PORT).allowed)

    def test_any_profile_rule_covers_everything(self):
        status = interpret(payload([rule(profile="Any"), pairing_rule(profile="Any"), pairing_tcp_rule(profile="Any")], [network("Public")]), EXE, PORT)
        self.assertTrue(status.covered)
        self.assertEqual(status.public_interfaces, ((5, "Wi-Fi"),))
        self.assertEqual(advise(status).action, "check")

    def test_bitmask_profile_is_understood(self):
        self.assertTrue(interpret(payload([rule(profile=2)]), EXE, PORT).covered)
        self.assertFalse(interpret(payload([rule(profile=4)]), EXE, PORT).covered)

    def test_public_hyper_v_switch_beside_private_wifi_is_not_the_lan(self):
        # A common setup: Wi-Fi Private with Internet, a vEthernet switch Public with none.
        networks = [network(), network("Public", alias="vEthernet (Hyper-V Switch)", index=31, ipv4="NoTraffic")]
        status = interpret(payload([rule(), pairing_rule(), pairing_tcp_rule()], networks), EXE, PORT)
        self.assertEqual(status.active_profiles, ("Private", "Public"))
        self.assertEqual(status.lan_profiles, ("Private",))
        self.assertTrue(status.covered)
        self.assertEqual(advise(status).action, "check")

    def test_domain_lan_widens_the_rule_to_domain_never_public(self):
        status = interpret(payload([rule()], [network("DomainAuthenticated", alias="Ethernet")]), EXE, PORT)
        advice = advise(status)
        self.assertEqual(advice.action, "repair")
        self.assertEqual(advice.rule_profiles, ("Private", "Domain"))

    def test_not_elevated_explains_instead_of_offering(self):
        status = interpret(payload(), EXE, PORT, elevated=False)
        advice = advise(status)
        self.assertIsNone(advice.action)
        self.assertIn("not running as administrator", advice.sentence)

    def test_error_offers_a_recheck(self):
        status = firewall_win.FirewallStatus(PORT, False, False, (), (), False, (), True, error="Access is denied")
        advice = advise(status)
        self.assertEqual(advice.action, "check")
        self.assertIn("Access is denied", advice.sentence)

    def test_status_off_windows_reports_an_error_not_a_crash(self):
        if firewall_win._IS_WINDOWS:
            self.skipTest("the Windows path shells out")
        status = firewall_win.status(EXE, PORT)
        self.assertTrue(status.error)
        self.assertFalse(status.allowed)


class RepairScriptTests(unittest.TestCase):
    def test_repair_never_opens_the_port_on_public(self):
        rendered = firewall_win._ADD_SCRIPT.format(
            exe=firewall_win._quote(EXE),
            name=firewall_win._quote(firewall_win.RULE_NAME), port=PORT,
            pairing_name=firewall_win._quote(firewall_win.PAIRING_RULE_NAME), pairing_port=firewall_win.PAIRING_PORT,
            pairing_tcp_name=firewall_win._quote(firewall_win.PAIRING_TCP_RULE_NAME),
            profiles="Private",
        )
        self.assertIn("-Profile Private ", rendered)
        self.assertNotIn("Public", rendered)
        self.assertIn(f"-Protocol TCP -LocalPort {PORT} ", rendered)
        self.assertIn(f"-Protocol UDP -LocalPort {firewall_win.PAIRING_PORT} ", rendered)
        self.assertIn(f"-DisplayName 'Beamer Pairing (TCP-In)' -Direction Inbound -Protocol TCP -LocalPort {firewall_win.PAIRING_PORT} ", rendered)
        self.assertEqual(rendered.count("-Profile Private "), 3)

    def test_the_removal_never_runs_bare(self):
        # A bare Remove-NetFirewallRule deletes every rule on the machine.
        rendered = firewall_win._REMOVE_SCRIPT.format(
            exe=firewall_win._quote(EXE), filters=firewall_win._RULES_FOR_LEAF,
            name=firewall_win._quote(firewall_win.RULE_NAME),
            pairing_name=firewall_win._quote(firewall_win.PAIRING_RULE_NAME),
            pairing_tcp_name=firewall_win._quote(firewall_win.PAIRING_TCP_RULE_NAME),
        )
        self.assertNotIn("\nRemove-NetFirewallRule", rendered)
        self.assertIn("foreach ($rule in $stale)", rendered)

    def test_quote_doubles_single_quotes(self):
        self.assertEqual(firewall_win._quote("it's"), "'it''s'")


NOT_FOUND = "Remove-NetFirewallRule : The requested object could not be found."


class RepairToleratesAbsentRulesTests(unittest.TestCase):
    """1.5.0-beta.2, fresh install on the rig (01-10-2026): a rule enumerated twice (once by its
    program, once by its display name) is removed once and then "not found", which aborted repair
    before any rule was written. A rule that is already gone is the state removal wants."""

    def run_repair(self, remove_error=None):
        calls = []

        def fake(script, timeout=60.0):
            calls.append(script)
            if "Remove-NetFirewallRule" in script and remove_error is not None:
                raise RuntimeError(remove_error)
            return ""

        with mock.patch.object(firewall_win, "_powershell", side_effect=fake):
            firewall_win.repair(EXE, PORT)
        return calls

    def test_a_rule_already_gone_does_not_stop_the_new_rules_being_written(self):
        calls = self.run_repair(NOT_FOUND)
        self.assertEqual(sum("New-NetFirewallRule" in call for call in calls), 1)
        self.assertEqual(calls[-1].count("New-NetFirewallRule"), 3)

    def test_any_other_removal_failure_still_fails_and_writes_nothing(self):
        calls = []

        def fake(script, timeout=60.0):
            calls.append(script)
            raise RuntimeError("Access is denied.")

        with mock.patch.object(firewall_win, "_powershell", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "Access is denied"):
                firewall_win.repair(EXE, PORT)
        self.assertFalse(any("New-NetFirewallRule" in call for call in calls))

    def test_the_removal_script_removes_each_rule_once_and_tolerates_not_found(self):
        removal = firewall_win._REMOVE_SCRIPT.format(
            exe=firewall_win._quote(EXE), filters=firewall_win._RULES_FOR_LEAF,
            name=firewall_win._quote(firewall_win.RULE_NAME),
            pairing_name=firewall_win._quote(firewall_win.PAIRING_RULE_NAME),
            pairing_tcp_name=firewall_win._quote(firewall_win.PAIRING_TCP_RULE_NAME),
        )
        self.assertIn("Sort-Object Name -Unique", removal)
        self.assertIn("could not be found", removal)
        self.assertNotIn("$stale | Remove-NetFirewallRule", removal)


if __name__ == "__main__":
    unittest.main()


class PairingRuleTests(unittest.TestCase):
    """An install made before pairing existed has the TCP rule and not the UDP one. Reporting
    only the TCP rule told that user everything was fine while no Mac could ever discover them."""

    def test_tcp_rule_without_the_pairing_rule_is_reported_and_repairable(self):
        status = interpret(payload([rule()]), EXE, PORT)
        self.assertTrue(status.allowed)
        self.assertFalse(status.pairing_allowed)
        advice = advise(status)
        self.assertEqual(advice.action, "repair")
        self.assertIn("pairing beacon", advice.sentence)

    def test_a_missing_pairing_rule_is_one_this_app_may_add_by_itself(self):
        # An upgrade from 1.4.x has the receiver and the UDP rule and no TCP one: the first code shown
        # would otherwise make Windows ask, and a click on Allow writes rules for public networks too.
        status = interpret(payload([rule(), pairing_rule()]), EXE, PORT)
        self.assertTrue(firewall_win.missing_rule(status))
        whole = interpret(payload([rule(), pairing_rule(), pairing_tcp_rule()]), EXE, PORT)
        self.assertFalse(firewall_win.missing_rule(whole))
        blocked = interpret(payload([rule(action="Block")]), EXE, PORT)
        self.assertFalse(firewall_win.missing_rule(blocked))

    def test_both_rules_present_is_all_clear(self):
        status = interpret(payload([rule(), pairing_rule(), pairing_tcp_rule()]), EXE, PORT)
        self.assertTrue(status.pairing_allowed)
        advice = advise(status)
        self.assertEqual(advice.action, "check")
        self.assertIn("discover this PC", advice.sentence)

    def test_a_disabled_pairing_rule_does_not_count(self):
        status = interpret(payload([rule(), pairing_rule(enabled="False")]), EXE, PORT)
        self.assertFalse(status.pairing_allowed)

    def test_a_tcp_rule_on_the_pairing_port_is_not_the_pairing_rule(self):
        wrong = rule(port=str(firewall_win.PAIRING_PORT))
        status = interpret(payload([rule(), wrong]), EXE, PORT)
        self.assertFalse(status.pairing_allowed)

    def test_pairing_rules_without_the_tcp_one_is_reported_and_repairable(self):
        # Installs from before pairing version 3 have the UDP beacon rule and not the TCP exchange.
        status = interpret(payload([rule(), pairing_rule()]), EXE, PORT)
        self.assertTrue(status.pairing_allowed)
        self.assertFalse(status.pairing_tcp_allowed)
        advice = advise(status)
        self.assertEqual(advice.action, "repair")
        self.assertIn(f"TCP {firewall_win.PAIRING_PORT}", advice.sentence)
        self.assertNotIn("UDP", advice.sentence)

    def test_both_pairing_rules_missing_names_both(self):
        advice = advise(interpret(payload([rule()]), EXE, PORT))
        self.assertIn(f"UDP {firewall_win.PAIRING_PORT}", advice.sentence)
        self.assertIn(f"TCP {firewall_win.PAIRING_PORT}", advice.sentence)

    def test_a_disabled_or_udp_pairing_tcp_rule_does_not_count(self):
        for wrong in (pairing_tcp_rule(enabled="False"), {**pairing_tcp_rule(), "protocol": "UDP"}):
            status = interpret(payload([rule(), pairing_rule(), wrong]), EXE, PORT)
            self.assertFalse(status.pairing_tcp_allowed)

    def test_a_pairing_tcp_block_is_not_an_allow(self):
        status = interpret(payload([rule(), pairing_rule(), pairing_tcp_rule(action="Block")]), EXE, PORT)
        self.assertFalse(status.pairing_tcp_allowed)

    def test_a_missing_tcp_rule_is_reported_before_the_pairing_rule(self):
        # Input not arriving at all is the bigger problem; one button fixes both.
        status = interpret(payload([pairing_rule()]), EXE, PORT)
        self.assertFalse(status.allowed)
        self.assertIn("no rule letting your Mac reach", advise(status).sentence)
