"""Colour choices stay stable across platforms and mixed-version links."""
import unittest

from core import effects, settings_sync
import tokens

NEW_IDS = ('aurora', 'opal', 'sea_glass', 'arc', 'magnesium', 'ruby', 'cobalt',
           'moss', 'plum', 'woad', 'saffron', 'citron', 'laser', 'electric', 'fuchsia', 'ultraviolet')
MONO = {
    'mono': ('White', ['#ebebeb', '#969696', '#ebebeb']),
    'mono_silver': ('Silver', ['#c9ced2', '#a8afb6', '#e1e5e8']),
    'mono_graphite': ('Graphite', ['#65696f', '#4a4f55', '#868c94']),
    'mono_ink': ('Ink', ['#25282c', '#101215', '#41464d']),
    'mono_warm': ('Warm grey', ['#b7ada0', '#8a8074', '#d8cfc2']),
}


def luminance(hex_value):
    channels = [int(hex_value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))


class ColourPacksTests(unittest.TestCase):
    def test_each_chromatic_group_offers_five_and_mono_has_a_neutral_home(self):
        groups = effects.colour_groups(tuple((key, key.capitalize()) for key in tokens.PALETTES))
        self.assertEqual([title for title, _ in groups],
                         ['Light', 'Mono', 'Membrane', 'Sparks', 'Instrument', 'Folio', 'Selvedge', 'Bright'])
        self.assertEqual(dict(groups)['Mono'], [(key, name) for key, (name, _) in MONO.items()])
        for title, choices in groups:
            self.assertEqual(len(choices), 5, title)
            if title != 'Mono':
                self.assertNotIn('mono', [value for value, _ in choices])

    def test_mono_stops_and_shared_resolver_preserve_the_spec(self):
        for value, expected in MONO.items():
            self.assertEqual(effects.pack(value), expected)
        self.assertEqual(effects.legible(MONO['mono_graphite'][1], True),
                         ['#6d7278', '#6b727b', '#868c94'])
        self.assertEqual(effects.legible(MONO['mono_ink'][1], True),
                         ['#69717d', '#637082', '#69717c'])
        self.assertEqual(effects.legible(MONO['mono'][1], False, effects.effect('aperture')),
                         ['#999999', '#969696', '#999999'])

    def test_new_colours_survive_both_sync_envelopes(self):
        for value in NEW_IDS + tuple(MONO):
            with self.subTest(value=value):
                state = {'set_at': 2, 'by': '', 'values': {'glow_colour': value}}
                data = settings_sync.message_data(True, 2, {'glow_colour': value}, design_state=state)
                self.assertEqual(settings_sync.read(data, {})[2]['glow_colour'], value)
                self.assertEqual(settings_sync.read_design_state(data, {})['values']['glow_colour'], value)

    def test_unknown_peer_colour_retains_the_current_choice_and_other_fields_apply(self):
        raw = {'crossing': {'glow_colour': 'mono', 'effect_length': 'normal'}}
        data = settings_sync.message_data(True, 2, {'glow_colour': 'future_colour', 'effect_length': 'long'})
        values = settings_sync.read(data, {})[2]
        raw = settings_sync.apply_mac(raw, values)
        self.assertEqual(raw['crossing']['glow_colour'], 'mono')
        self.assertEqual(raw['crossing']['effect_length'], 'long')

    def test_new_packs_keep_three_to_one_contrast_on_light_and_dark(self):
        for value in NEW_IDS:
            stops = tokens.PALETTES[value] if value in tokens.PALETTES else effects.pack(value)[1]
            for dark, background in ((True, tokens.PALETTE['well']), (False, '#ffffff'),
                                     (False, tokens.PALETTE_LIGHT['well'])):
                for stop in effects.legible(stops, dark):
                    a, b = sorted((luminance(stop), luminance(background)))
                    self.assertGreaterEqual((b + .05) / (a + .05), 3, (value, dark, stop))

    def test_saved_original_palettes_and_packs_resolve_to_the_same_stops(self):
        self.assertEqual(tokens.PALETTES['mono'], ('#ebebeb', '#969696', '#ebebeb'))
        for value, stops in (('vellum', ['#c69a60', '#f0ddac', '#88674c']),
                             ('phosphor', ['#3dff7f', '#1f9e52', '#caffdc']),
                             ('tide', ['#58a9a3', '#547498', '#b1d1c1'])):
            self.assertEqual(effects.pack(value)[1], stops)
