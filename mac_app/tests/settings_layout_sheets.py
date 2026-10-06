"""Contact sheets retain every document pixel at a logical 1x inspection scale."""
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def make_sheets(root):
    destination = root / 'contact-sheets'
    destination.mkdir(exist_ok=True)
    font = ImageFont.truetype('/System/Library/Fonts/Helvetica.ttc', 16)
    records = json.loads((root / 'measurements.json').read_text())
    index = []

    def picture(path):
        image = Image.open(path).convert('RGBA')
        ground = '#0e0f0d' if root.name == 'dark' else '#f1f0ea'
        return Image.alpha_composite(Image.new('RGBA', image.size, ground), image).convert('RGB')

    for record in records:
        page, width = record['page'], round(record['frame'][2])
        cell_width = min(width, 900)
        cell_height = round(872 * cell_width / width)
        sheet = Image.new('RGB', (3 * cell_width, 2 * (cell_height + 28)), '#31312d')
        draw = ImageDraw.Draw(sheet)
        originals = []
        for row, density in enumerate((1, 2)):
            for col, position in enumerate(('top', 'middle', 'bottom')):
                source = root / f'{page}-{width}-{density}x-{position}.png'
                resized = picture(source).resize((cell_width, cell_height), Image.Resampling.LANCZOS)
                x, y = col * cell_width, row * (cell_height + 28)
                sheet.paste(resized, (x, y + 28))
                draw.text((x + 8, y + 5), f'{page} {width} pt {density}x {position}', fill='white', font=font)
                originals.append(source.name)
        name = f'{page}-{width}-viewports.png'
        sheet.save(destination / name)
        index.append({'sheet': name, 'originals': originals})
        logical = Image.open(root / f'{page}-{width}-1x-document.png')
        height = logical.height
        left, _, column_width, _ = record['column']
        left, right = max(0, math.floor(left) - 4), min(logical.width, math.ceil(left + column_width) + 4)
        tile_width = right - left
        for start in range(0, height, 1400):
            chunks = list(range(start, min(height, start + 1400), 700))
            sheet = Image.new('RGB', (len(chunks) * tile_width, 1456), '#31312d')
            draw = ImageDraw.Draw(sheet)
            originals = []
            for row, density in enumerate((1, 2)):
                source = root / f'{page}-{width}-{density}x-document.png'
                original = picture(source)
                for col, top in enumerate(chunks):
                    bottom = min(height, top + 700)
                    strip = original.crop((left * density, top * density, right * density, bottom * density))
                    strip = strip.resize((tile_width, bottom - top), Image.Resampling.LANCZOS)
                    x, y = col * tile_width, row * 728
                    sheet.paste(strip, (x, y + 28))
                    draw.text((x + 5, y + 5), f'{page} {width} pt {density}x y{top}', fill='white', font=font)
                originals.append(source.name)
            name = f'{page}-{width}-document-{start:05}.png'
            sheet.save(destination / name)
            index.append({'sheet': name, 'originals': originals})
    (destination / 'index.json').write_text(json.dumps(index, indent=2) + '\n')
    print(root.name, len(index), 'sheets covering', len({s for entry in index for s in entry['originals']}), 'renders')


if __name__ == '__main__':
    for name in ('dark', 'light'):
        make_sheets(Path(sys.argv[1]) / name)
