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
        self.assertEqual((config.glow_style, config.glow_colour), ("glow", "colourful"))
        existing = config_from_dict({**BASE, "glow_colour": "signal"})
        self.assertEqual(existing.glow_colour, "signal")
        config = config_from_dict({**BASE, "glow_style": "beam", "glow_colour": "sunset"})
        saved = config_to_dict(config)
        self.assertEqual((saved["glow_style"], saved["glow_colour"]), ("beam", "sunset"))

    def test_colour_choices_are_the_shared_palettes_then_the_effect_packs(self):
        from core import effects
        import tokens

        self.assertEqual(app_config.GLOW_COLOURS, tuple(tokens.PALETTES) + effects.PACK_IDS)

    def test_all_original_saved_colours_keep_their_id_and_stops(self):
        from pathlib import Path
        import json
        fixture = Path(__file__).resolve().parents[2] / 'core/tests/colour_packs_rc3.json'
        for value, stops in json.loads(fixture.read_text()).items():
            with self.subTest(value=value):
                saved = config_to_dict(config_from_dict({**BASE, 'glow_colour': value}))
                loaded = config_from_dict(saved)
                self.assertEqual(loaded.glow_colour, value)
                self.assertEqual(app_config.palette_colours(loaded.glow_colour), tuple(stops))

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
        config = config_from_dict({**BASE, "glow_style": "aperture", "glow_colour": "marbled"})
        self.assertEqual((config.glow_style, config.glow_colour), ("aperture", "marbled"))

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

    def test_effect_size_defaults_to_medium_and_round_trips(self):
        self.assertEqual(config_from_dict(dict(BASE)).effect_size, "medium")
        config = config_from_dict({**BASE, "effect_size": "large"})
        self.assertEqual(config_to_dict(config)["effect_size"], "large")
        with self.assertRaises(ConfigError):
            config_from_dict({**BASE, "effect_size": "huge"})

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

    def test_migrating_a_config_on_the_old_port_writes_the_move_down(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "config.json").write_text(
                json.dumps(
                    {"host": "192.168.1.3", "port": protocol.LEGACY_DEFAULT_PORT, "auth_token": "t"}
                ),
                encoding="utf-8",
            )
            path = Path(directory) / "settings.json"
            self.assertEqual(app_config.load_config(path).port, protocol.DEFAULT_PORT)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["port"], protocol.DEFAULT_PORT)


class ReadFallbackPeerBookTests(unittest.TestCase):
    """A read that fails (a scanner holding settings.json) gives the link's read-only calls the last
    good copy, but never a copy that a save could write back over what the window has since changed."""

    def setUp(self):
        self.disk = {"peers": [{"id": "a", "token": "t", "paired_with": []}], "zones": [{"peer": "a"}]}
        self.broken = False
        self.saved = []

        def load():
            if self.broken:
                raise OSError("locked by a scanner")
            return json.loads(json.dumps(self.disk))

        self.book = app_config.ReadFallbackPeerBook(load, self.saved.append)

    def test_the_read_only_calls_use_the_last_good_copy_when_a_read_fails(self):
        self.book.peers()
        self.broken = True
        self.assertEqual([peer["id"] for peer in self.book.peers()], ["a"])
        self.assertEqual(self.book.zones(), [{"peer": "a"}])

    def test_with_no_good_copy_yet_a_failed_read_raises(self):
        self.broken = True
        with self.assertRaises(OSError):
            self.book.peers()

    def test_a_save_after_a_failed_read_raises_rather_than_write_the_stale_copy(self):
        self.book.peers()
        self.disk["peers"][0]["send"] = False
        self.broken = True
        with self.assertRaises(OSError):
            self.book.store_paired("a", ["b"])
        with self.assertRaises(OSError):
            self.book.admit(b"\x00" * 32, b"\x01" * 16, b"\x02" * 16, {})
        self.assertEqual(self.saved, [])

    def test_a_removed_machine_does_not_come_back_through_a_pairing_message(self):
        self.book.peers()
        self.disk["peers"] = []
        self.broken = True
        with self.assertRaises(OSError):
            self.book.store_paired("a", ["b"])
        self.assertEqual(self.saved, [])
