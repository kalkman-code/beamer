import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app_config
from core import protocol
from dataclasses import replace

from app_config import ConfigError, config_from_dict, config_to_dict, default_config


BASE = {"host": "192.168.1.3", "port": 51820, "auth_token": "shared-token"}


class EdgeGlowConfigTests(unittest.TestCase):
    def test_defaults_on_and_round_trips(self):
        self.assertTrue(config_from_dict(dict(BASE)).edge_glow)
        config = config_from_dict({**BASE, "edge_glow": False})
        self.assertFalse(config.edge_glow)
        self.assertIs(config_to_dict(config)["edge_glow"], False)

    def test_rejects_a_non_boolean(self):
        with self.assertRaises(ConfigError):
            config_from_dict({**BASE, "edge_glow": "yes"})

    def test_showing_where_the_pointer_lands_is_on_for_an_old_config_and_round_trips_off(self):
        self.assertTrue(config_from_dict(dict(BASE)).shortcut_arrival)
        config = config_from_dict({**BASE, "shortcut_arrival": False})
        self.assertIs(config_to_dict(config)["shortcut_arrival"], False)
        with self.assertRaises(ConfigError):
            config_from_dict({**BASE, "shortcut_arrival": "yes"})

    def test_what_a_switch_plays_defaults_to_the_crossing_and_round_trips(self):
        self.assertEqual(config_from_dict(dict(BASE)).shortcut_arrival_style, "match")
        config = config_from_dict({**BASE, "shortcut_arrival_style": "discharge"})
        self.assertEqual(config_to_dict(config)["shortcut_arrival_style"], "discharge")
        with self.assertRaises(ConfigError):
            config_from_dict({**BASE, "shortcut_arrival_style": "sparkle"})

    def test_style_and_colour_default_to_the_original_look_and_round_trip(self):
        config = config_from_dict(dict(BASE))
        self.assertEqual((config.glow_style, config.glow_colour), ("glow", "signal"))
        config = config_from_dict({**BASE, "glow_style": "beam", "glow_colour": "sunset"})
        saved = config_to_dict(config)
        self.assertEqual((saved["glow_style"], saved["glow_colour"]), ("beam", "sunset"))

    def test_colour_choices_are_the_shared_palettes_then_the_effect_packs(self):
        from core import effects
        import tokens

        self.assertEqual(app_config.GLOW_COLOURS, tuple(tokens.PALETTES) + effects.PACK_IDS)

    def test_every_crossing_effect_and_pack_is_accepted_and_round_trips(self):
        from core import effects

        for style in effects.EFFECT_IDS:
            with self.subTest(style=style):
                saved = config_to_dict(config_from_dict({**BASE, "glow_style": style}))
                self.assertEqual(config_from_dict(saved).glow_style, style)
        for colour in effects.PACK_IDS:
            with self.subTest(colour=colour):
                saved = config_to_dict(config_from_dict({**BASE, "glow_colour": colour}))
                self.assertEqual(config_from_dict(saved).glow_colour, colour)

    def test_any_colour_goes_with_any_style(self):
        config = config_from_dict({**BASE, "glow_style": "glow", "glow_colour": "ember"})
        self.assertEqual((config.glow_style, config.glow_colour), ("glow", "ember"))
        config = config_from_dict({**BASE, "glow_style": "discharge", "glow_colour": "sunset"})
        self.assertEqual((config.glow_style, config.glow_colour), ("discharge", "sunset"))

    def test_a_colour_resolves_to_todays_palette_else_its_pack(self):
        from core import effects
        import tokens

        self.assertEqual(app_config.palette_colours("sunset"), tuple(tokens.PALETTES["sunset"]))
        self.assertEqual(app_config.palette_colours("forge"), tuple(effects.pack("forge")[1]))
        self.assertEqual(app_config.palette_colours("tartan"), tuple(tokens.PALETTES["signal"]))

    def test_the_length_defaults_to_normal_and_round_trips(self):
        self.assertEqual(config_from_dict(dict(BASE)).effect_length, "normal")
        config = config_from_dict({**BASE, "effect_length": "long"})
        self.assertEqual(config_to_dict(config)["effect_length"], "long")
        with self.assertRaises(ConfigError):
            config_from_dict({**BASE, "effect_length": "forever"})

    def test_a_saved_warp_choice_reads_as_its_stand_in(self):
        # Warp is offered nowhere for now; a PC that chose it keeps working.
        config = config_from_dict({**BASE, "glow_style": "light_slit", "glow_colour": "neon",
                                   "shortcut_arrival_style": "wormhole"})
        self.assertEqual((config.glow_style, config.glow_colour, config.shortcut_arrival_style),
                         ("beam", "colourful", "match"))
        self.assertEqual(config_from_dict({**BASE, "glow_style": "wormhole"}).glow_style, "glow")
        # Ink likewise.
        config = config_from_dict({**BASE, "glow_style": "capillary", "glow_colour": "matcha",
                                   "shortcut_arrival_style": "sumi_bloom"})
        self.assertEqual((config.glow_style, config.glow_colour, config.shortcut_arrival_style),
                         ("glow", "colourful", "match"))

    def test_rejects_an_unknown_style_or_colour(self):
        for key, value in (("glow_style", "sparkle"), ("glow_colour", "tartan")):
            with self.subTest(key=key), self.assertRaises(ConfigError) as caught:
                config_from_dict({**BASE, key: value})
            self.assertIn(key, str(caught.exception))


class AppearanceConfigTests(unittest.TestCase):
    def test_defaults_to_system(self):
        self.assertEqual(config_from_dict(dict(BASE)).appearance, "system")

    def test_round_trips_light_and_dark(self):
        for choice in ("light", "dark"):
            config = config_from_dict({**BASE, "appearance": choice})
            self.assertEqual(config_to_dict(config)["appearance"], choice)

    def test_a_bad_value_falls_back_to_system_instead_of_erroring(self):
        self.assertEqual(config_from_dict({**BASE, "appearance": "sepia"}).appearance, "system")



class HandEditedListsTests(unittest.TestCase):
    def test_an_unhashable_entry_is_a_config_error_not_a_crash(self):
        base = {"host": "192.0.2.10", "port": 24820, "auth_token": "synthetic-token-for-tests"}
        for bad in ([["middle"]], [{}]):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                config_from_dict(dict(base, crossing_edge_parts=bad))
        with self.assertRaises(ConfigError):
            config_from_dict(dict(base, crossing_methods=[["edge"]]))



class HideAddressesTests(unittest.TestCase):
    def test_off_by_default_round_trips_and_refuses_a_non_bool(self):
        base = {"host": "192.0.2.10", "port": 24820, "auth_token": "synthetic-token-for-tests"}
        self.assertFalse(config_from_dict(base).hide_addresses)
        config = config_from_dict(dict(base, hide_addresses=True))
        self.assertTrue(config_to_dict(config)["hide_addresses"])
        with self.assertRaises(ConfigError):
            config_from_dict(dict(base, hide_addresses="yes"))


if __name__ == "__main__":
    unittest.main()


class ArrangementTests(unittest.TestCase):
    """Which end's arrangement stands when the two disagree."""

    def test_the_newer_stamp_wins(self):
        self.assertTrue(protocol.arrangement_wins(200, 100))
        self.assertFalse(protocol.arrangement_wins(100, 200))

    def test_an_equal_stamp_changes_nothing(self):
        self.assertFalse(protocol.arrangement_wins(100, 100))

    def test_a_peer_with_no_stamp_never_beats_a_change_made_here(self):
        self.assertFalse(protocol.arrangement_wins(0, 100))
        self.assertFalse(protocol.arrangement_wins(None, 100))

    def test_two_ends_that_never_changed_it_agree(self):
        self.assertFalse(protocol.arrangement_wins(0, 0))


class LegacyTriggerTests(unittest.TestCase):
    def test_right_alt_is_a_choice_and_is_kept(self):
        # "alt_r" was once rewritten to "cmd_r" as a dead default, but it is a live entry in
        # TRIGGER_KEYS that the window offers, so a person who picked it lost it on every load.
        config = config_from_dict(
            {"host": "192.168.1.3", "port": 51820, "auth_token": "t", "trigger_key": "alt_r"}
        )
        self.assertEqual(config.trigger_key, "alt_r")

    def test_a_fresh_install_has_no_address_and_still_saves(self):
        config = default_config()
        self.assertEqual(config.host, "")
        config_to_dict(replace(config, auth_token="t"))

    def test_a_key_someone_chose_is_left_alone(self):
        config = config_from_dict(
            {"host": "192.168.1.3", "port": 51820, "auth_token": "t", "trigger_key": "shift_r"}
        )
        self.assertEqual(config.trigger_key, "shift_r")


class LegacyPortTests(unittest.TestCase):
    def test_the_old_default_port_is_moved_out_of_the_ephemeral_range(self):
        config = config_from_dict(
            {"host": "192.168.1.3", "port": protocol.LEGACY_DEFAULT_PORT, "auth_token": "t"}
        )
        self.assertEqual(config.port, protocol.DEFAULT_PORT)
        self.assertLess(protocol.DEFAULT_PORT, 49152)
        self.assertLess(protocol.PAIRING_PORT, 49152)

    def test_a_port_someone_chose_is_left_alone(self):
        config = config_from_dict({"host": "192.168.1.3", "port": 9000, "auth_token": "t"})
        self.assertEqual(config.port, 9000)

    def test_a_new_config_starts_on_the_new_port(self):
        self.assertEqual(default_config().port, protocol.DEFAULT_PORT)

    def test_loading_a_config_on_the_old_port_writes_the_move_down(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {"host": "192.168.1.3", "port": protocol.LEGACY_DEFAULT_PORT, "auth_token": "t"}
                ),
                encoding="utf-8",
            )
            self.assertEqual(app_config.load_config(path).port, protocol.DEFAULT_PORT)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["port"], protocol.DEFAULT_PORT)

