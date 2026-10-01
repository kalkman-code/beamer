import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import link_state
import widgets
from bridge_fakes import PAIRED_TOKEN, FakeClock, FakeQuartz, make_config, quiet_logger
from fake_link import FakeLink, bring_up
from core.link import connection_ended
from wake import WakingController, not_woken_status


def controller(status="Connected to 192.168.1.3:51820", connected=True, kind="connected", **overrides):
    values = dict(
        cfg=SimpleNamespace(host="192.168.1.3", port=51820, auth_token="t", pc_name="STUDIO-PC"),
        connection_status=status,
        status_kind=kind,
        via_tunnel=False,
        connected=connected,
        waking=False,
        windows_locked=False,
        redirecting=False,
        crossing=SimpleNamespace(armed=True),
        crossing_paused=False,
        full_screen_app=None,
        receiving=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class ErrnoError(OSError):
    pass


class LinkStateTest(unittest.TestCase):
    def assertState(self, expected_key, tone, **kwargs):
        state = link_state.describe(controller(**kwargs))
        self.assertEqual((state.key, state.tone), (expected_key, tone))
        self.assertTrue(state.word and state.tag and state.detail)

    def test_the_pc_driving_this_mac_beats_every_other_state(self):
        # Whatever this Mac's own link is doing, the keyboard in the hand is
        # the PC's: nothing else the status could say is more true than that.
        self.assertState("receiving", "signal", receiving=True)
        self.assertState("receiving", "signal", receiving=True, redirecting=True)
        self.assertState("receiving", "signal", receiving=True, connected=False)

    def test_the_states_name_the_machine_they_are_about(self):
        driving = link_state.describe(controller(receiving=True, driver_label="Laptop", peer_label="Desk"))
        self.assertEqual(driving.word, "From Laptop")
        self.assertIn("Laptop's keyboard", driving.detail)
        sending = link_state.describe(controller(redirecting=True, on_label="Laptop", peer_label="Desk"))
        self.assertEqual(sending.word, "On Laptop")
        self.assertIn("Laptop", sending.detail)
        idle = link_state.describe(controller(peer_label="Desk (AAAA)"))
        self.assertIn("Desk (AAAA)", idle.detail)

    def test_with_no_label_the_saved_name_stands_in_and_nothing_says_windows(self):
        cfg = SimpleNamespace(host="192.168.1.3", port=51820, auth_token="t", pc_name="")
        words = [link_state.describe(controller(cfg=cfg, **kwargs)) for kwargs in (
            dict(receiving=True), dict(redirecting=True), dict(waking=True, connected=False),
            dict(connected=False, kind="unreachable"), dict(connected=False, kind="refused"))]
        for state in words:
            self.assertNotIn("Windows", state.word + state.detail)
            self.assertNotIn("PC", state.word + state.detail)
        bare = SimpleNamespace(host="", port=51820, auth_token="t", pc_name="")
        self.assertEqual(link_state.peer_name(bare), "the other machine")

    def test_a_refused_token_says_remove_then_pair_again(self):
        state = link_state.describe(controller(kind="unauthenticated", connected=False))
        self.assertIn("Remove it, then pair again", state.detail)

    def test_connected_states(self):
        self.assertState("mac", "ink")
        self.assertState("windows", "signal", redirecting=True)
        self.assertState("unlocking", "amber", status="Unlocking STUDIO-PC…", windows_locked=True, redirecting=True)
        self.assertState("paused", "ink", crossing_paused=True)
        self.assertState("full_screen", "ink", full_screen_app="Final Cut Pro")
        self.assertState("waking", "amber", waking=True, connected=False)

    def test_tunnel_is_tagged(self):
        state = link_state.describe(controller(via_tunnel=True))
        self.assertEqual((state.key, state.tag), ("mac", "Tunnel"))
        self.assertIn("tunnel", state.detail)

    def test_every_failure_kind_has_a_word(self):
        ended = lambda number, name="Windows": connection_ended(OSError(number, "x"), name)
        cases = [
            ("unauthenticated", "Windows could not be authenticated", "token"),
            ("wrong_id", "Windows has this pairing under another machine", "token"),
            ("different", "A different Beamer answered", "token"),
            ("unreadable", "Windows could not read what this machine sent first", "token"),
            ("older", "Update Beamer on Windows: it is older than this one", "version"),
            ("newer", "Windows runs a newer Beamer", "version"),
            ("not_beamer", "Windows did not answer as a Beamer", "version"),
            ("failed", not_woken_status("Windows"), "not_woken"),
            ("failed", not_woken_status("Desk (AAAA)"), "not_woken"),
            (ended(65).kind, ended(65).text, "unreachable"),
            (ended(51).kind, ended(51).text, "unreachable"),
            (connection_ended(TimeoutError(), "Windows").kind, connection_ended(TimeoutError(), "Windows").text, "unreachable"),
            ("blocked", "macOS 27 blocked direct LAN access. Open /Applications/Beamer Tunnel.command.", "blocked"),
            (ended(61).kind, ended(61).text, "not_listening"),
            ("stopped", "Windows stopped responding", "stopped"),
            ("closed", "Windows closed the connection", "stopped"),
            ("failed", "Connecting to 192.168.1.3:51820", "waiting"),
            ("failed", "Waiting to connect", "waiting"),
            ("failed", "Settings saved; reconnecting", "waiting"),
            ("failed", "Connection failed: broken pipe", "dropped"),
            ("failed", "", "dropped"),
            ("off", "This link was stopped", "dropped"),
            ("none", "", "dropped"),
        ]
        for kind, status, key in cases:
            with self.subTest(kind=kind, status=status):
                self.assertState(key, "amber" if key == "waiting" else "fault", kind=kind, status=status, connected=False)

    def test_the_word_comes_from_the_kind_not_the_sentence(self):
        self.assertState("token", "fault", kind="unauthenticated", status="Something this build never said", connected=False)
        self.assertState("dropped", "fault", kind="failed", status="Windows is unreachable from this machine.", connected=False)

    def test_a_stale_failure_kind_is_ignored_while_connected(self):
        self.assertState("mac", "ink", kind="refused")

    def test_unpaired(self):
        cfg = SimpleNamespace(host="192.168.1.3", port=51820, auth_token="", pc_name="")
        self.assertEqual(link_state.describe(controller(connected=False, kind="refused", cfg=cfg)).key, "unpaired")


class ARealControllerTest(unittest.TestCase):
    def test_the_primary_links_kind_reaches_the_word(self):
        real = WakingController(make_config(PAIRED_TOKEN), logger=quiet_logger(), quartz=FakeQuartz, clock=FakeClock(),
                             link_factory=FakeLink, desktop_bounds=lambda: (0, 0, 1920, 1080))
        self.assertEqual(link_state.describe(real).key, "waiting")
        link = bring_up(real)
        self.assertEqual(link_state.describe(real).key, "mac")
        link.down(real, "Office PC is reachable, but Beamer is not listening on this port.", "refused")
        self.assertEqual(real.status_kind, "refused")
        self.assertEqual(link_state.describe(real).key, "not_listening")
        link.down(real, "Update Beamer on Office PC: it is older than this one", "older")
        self.assertEqual(link_state.describe(real).key, "version")


class RulerScaleTest(unittest.TestCase):
    def test_square_root_gives_the_useful_band_half_the_travel(self):
        low = widgets.scale_fraction(150, 50, 1000, "sqrt")
        high = widgets.scale_fraction(600, 50, 1000, "sqrt")
        self.assertGreaterEqual(high - low, 0.49)

    def test_round_trip_and_pinning(self):
        for value in (50, 300, 1000):
            fraction = widgets.scale_fraction(value, 50, 1000, "sqrt")
            self.assertAlmostEqual(widgets.scale_value(fraction, 50, 1000, "sqrt"), value)
        self.assertEqual(widgets.scale_fraction(2000, 50, 1000, "sqrt"), 1.0)
        self.assertEqual(widgets.scale_fraction(250, 0, 500), 0.5)


if __name__ == "__main__":
    unittest.main()
