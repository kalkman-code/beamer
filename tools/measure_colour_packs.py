"""Write the palette proposal from measured sRGB stops and the pinned baseline."""
import colorsys
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import effects
import tokens


def rgb(value):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def linear(value):
    return tuple(c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb(value))


def luminance(value):
    return sum(c * w for c, w in zip(linear(value), (.2126, .7152, .0722)))


def contrast(a, b):
    lo, hi = sorted((luminance(a), luminance(b)))
    return (hi + .05) / (lo + .05)


def lab(value):
    r, g, b = linear(value)
    xyz = ((r * .4124564 + g * .3575761 + b * .1804375) / .95047,
           r * .2126729 + g * .7151522 + b * .0721750,
           (r * .0193339 + g * .1191920 + b * .9503041) / 1.08883)
    x, y, z = (v ** (1 / 3) if v > .008856 else 7.787 * v + 16 / 116 for v in xyz)
    return 116 * y - 16, 500 * (x - y), 200 * (y - z)


def distance(a, b):
    return math.dist(lab(a), lab(b))


def baseline():
    def source(path):
        return subprocess.check_output(['git', 'show', f'4370f9b:{path}'], cwd=ROOT, text=True)
    namespace = {}
    exec(source('tokens.py'), namespace)
    old = {'Light': {key: (key.capitalize(), list(stops)) for key, stops in namespace['PALETTES'].items()}}
    for module, title, _styles, _ids in effects.DIRECTIONS:
        namespace = {'__name__': f'core.{module}', '__package__': 'core'}
        exec(source(f'core/{module}.py'), namespace)
        old[title] = namespace['PACKS']
    return old


def main():
    old = baseline()
    groups = effects.colour_groups([(key, key.capitalize()) for key in tokens.PALETTES])
    old_ids = {value for choices in old.values() for value in choices}
    old_stops = [stop for choices in old.values() for _name, stops in choices.values() for stop in stops]
    lines = ['# Beamer colour proposal', '', '05-10-2026 · colours · baseline 4370f9b', '',
             'Keep every existing id and stop. Offer five chromatic choices in Light, Membrane, Sparks, Instrument, Folio and Selvedge. Add Bright with five vivid packs. Put the unchanged Mono choice in a separate Neutral row immediately after Light, available with every style. A single neutral row avoids duplicating the same selected id across seven groups and stops implying Mono belongs only to Light.', '',
             '## Current sets', '', '| Group | Count | Choices |', '| --- | ---: | --- |']
    for title, choices in old.items():
        lines.append(f'| {title} | {len(choices)} | ' + '; '.join(f'{name} (`{value}`): ' + ', '.join(stops) for value, (name, stops) in choices.items()) + ' |')
    lines += ['', 'Ink and Warp are deliberately hidden; their legacy mappings remain unchanged. Colour packs are independent of styles: each colour works with every style.', '',
              '## Proposed sets', '', '| Group | Five choices (or one neutral) |', '| --- | --- |']
    for title, choices in groups:
        lines.append(f'| {title} | ' + ', '.join(name + (' **new**' if value not in old_ids else '') for value, name in choices) + ' |')
    lines += ['', '## New stops and measurements', '',
              'Ratios below use opaque sRGB stops. Raw references are #161714 (dark panel) and #ffffff (white); rendered references are the actual preview wells, #1f201c (dark) and #e5e4dc (light). White is a reference, not the weakest possible light background. Hue is HSV in degrees for the first, characteristic stop; spacing is the smallest circular distance to a sibling’s first stop, ignoring achromatic stops. ΔE76 compares that stop with every original stop: larger means more separated in CIELAB. Gradients, bloom and opacity change final pixel contrast; these figures describe solid colour, not a claim that every antialiased pixel meets WCAG.', '',
              '| Group / new colour | All raw stops | Hue / nearest sibling spacing | Nearest old stop ΔE76 | Minimum raw dark panel / white contrast | Minimum rendered dark / light well contrast |',
              '| --- | --- | --- | ---: | --- | --- |']
    records = []
    for title, choices in groups:
        palettes = {value: list(tokens.PALETTES[value]) if value in tokens.PALETTES else effects.pack(value)[1] for value, _name in choices}
        for value, name in choices:
            if value in old_ids:
                continue
            stops = palettes[value]
            hue = colorsys.rgb_to_hsv(*rgb(stops[0]))[0] * 360
            sibling_hues = [colorsys.rgb_to_hsv(*rgb(p[0]))[0] * 360 for key, p in palettes.items()
                            if key != value and colorsys.rgb_to_hsv(*rgb(p[0]))[1] > .1]
            spacing = min(min(abs(hue - h), 360 - abs(hue - h)) for h in sibling_hues)
            raw = [min(contrast(stop, bg) for stop in stops) for bg in ('#161714', '#ffffff')]
            rendered = [min(contrast(stop, bg) for stop in effects.legible(stops, dark, effects.effect('crease')))
                        for dark, bg in ((True, tokens.PALETTE['well']), (False, tokens.PALETTE_LIGHT['well']))]
            delta = min(distance(stops[0], stop) for stop in old_stops)
            lines.append(f'| {title} / {name} (`{value}`) | ' + ', '.join(stops) +
                         f' | {hue:.1f}° / {spacing:.1f}° | {delta:.1f} | {raw[0]:.2f}:1 / {raw[1]:.2f}:1 | {rendered[0]:.2f}:1 / {rendered[1]:.2f}:1 |')
            records.append(dict(group=title, id=value, hue=hue, spacing=spacing, delta_e=delta, raw=raw, rendered=rendered))
    lines += ['', 'Membrane adds cool mineral Opal and green Sea glass; Sparks adds blue electrical Arc and violet Magnesium; Instrument adds red Ruby and blue Cobalt; Folio adds muted plant Moss and purple Plum; Selvedge adds dyed Woad and golden Saffron. Aurora adds a cool green/violet/teal sweep to Light.', '',
              'Bright uses narrow saturated hue bands rather than multicolour rainbow sweeps. Its characteristic hues are separated by at least %.1f°. Existing packs already span the entire hue wheel, so exclusive new hues are impossible; distinctness comes from hue, saturation, lightness and the complete ordered stop signature. No new stop duplicates an existing stop.' % min(r['spacing'] for r in records if r['group'] == 'Bright'), '',
              'A concrete limit: high luminance and 3:1 against white cannot coexist above relative luminance 0.30. Bright therefore uses the luminous raw stops on dark screens. All sixteen new choices have explicit hue-preserving darker stops on light screens, at least 3:1 on the actual preview wells and 4.2:1 on white. Five new shadow stops are also lifted slightly so every new colour exceeds 3:1 on the dark well. This does not alter older colours. Names and selection rings remain the non-colour cues.', '',
              '| New colour | All light-screen stops |', '| --- | --- |']
    for title, choices in groups:
        for value, name in choices:
            if value not in old_ids:
                stops = tokens.PALETTES[value] if value in tokens.PALETTES else effects.pack(value)[1]
                lines.append(f'| {name} | ' + ', '.join(effects.legible(stops, False)) + ' |')
    bright_stops = [stop for _name, stops in effects.BRIGHT_PACKS.values() for stop in stops]
    lines += ['', 'Across all fifteen Bright stops: HLS saturation is 100%%; relative luminance ranges from %.3f to %.3f; the nearest original stop is ΔE76 %.1f away. These are measurements in specified colour spaces, not a guarantee of separation for every form of colour-vision deficiency.' %
              (min(map(luminance, bright_stops)), max(map(luminance, bright_stops)),
               min(distance(stop, old_stop) for stop in bright_stops for old_stop in old_stops))]
    lines += ['', '## Compatibility and merge boundary', '',
              'Colours travel by stable id in settings.design.glow_colour and design_sync.values.glow_colour. The origin/release/1.5.0 peer (00c92e8) discards unsupported individual fields, retains its current colour, and applies supported fields. Its links do not fail. Runtime palette resolvers fall back to Signal if handed an unknown id directly. Keep these semantics; a new id must not force an arbitrary old colour onto an older peer.', '',
              'The shared catalogue lives in core/effects.py; adapters are mac_app/pages.py and win_app/pages_win.py. No layout, hover or selection code changes in the six files owned by design2. New light-screen remapping happens in the shared legible() path used by both native renderers. The Mac retains the legacy five-entry GLOW_COLOURS tuple because its existing swatch code derives the column count from it; Aurora is offered by pages.py and separately admitted by settings_store.py, avoiding an empty sixth column without changing layout code.']
    (ROOT / 'proposal.md').write_text('\n'.join(lines) + '\n')
    (ROOT / 'colour-measurements.json').write_text(json.dumps(records, indent=2) + '\n')
    for record in records:
        print(f"{record['id']}: hue {record['hue']:.1f}, spacing {record['spacing']:.1f}, old ΔE {record['delta_e']:.1f}, rendered {record['rendered']}")


if __name__ == '__main__':
    main()
