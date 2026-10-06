"""Synthetic, unordered AppKit layout measurements and density-controlled renders."""
import json
from pathlib import Path
from unittest import mock

from mac_app.tests.design_offscreen import AppKit, offscreen_control_window, rect_list, settle
from mac_app.tests.test_settings_layout_acceptance import frame_in, walk
import theme


def render(view, density, path=None):
    width, height = view.bounds().size
    bitmap = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, round(width * density), round(height * density), 8, 4, True, False,
        AppKit.NSDeviceRGBColorSpace, 0, 0)
    bitmap.setSize_((width, height))
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), bitmap)
    if path is not None:
        bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}).writeToFile_atomically_(str(path), True)
    return bitmap


def measure(control, key):
    scroll = control.pages[key]
    page = scroll.documentView()
    body = page.subviews()[0]
    children = []
    for child in walk(body):
        rect = frame_in(child, body)
        if rect.origin.x < -1 or rect.origin.x + rect.size.width > body.bounds().size.width + 1:
            children.append(dict(kind=type(child).__name__, text=str(child.stringValue()) if isinstance(child, AppKit.NSTextField) else str(child.accessibilityLabel()),
                                 rect=rect_list(rect), parent=type(child.superview()).__name__))
    return dict(page=key, frame=rect_list(control.window.frame()), sidebar=rect_list(control.sidebar.view.frame()),
                viewport=rect_list(scroll.contentView().bounds()), document=rect_list(page.frame()),
                column=rect_list(body.frame()), sections=[rect_list(frame_in(child, body)) for child in body.arrangedSubviews() if not child.isHidden()],
                overflows=children, font=str(theme.font(theme.TYPE['body']).fontName()),
                controls={name: rect_list(frame_in(getattr(control, name).view, body)) for name in
                          ('length_select', 'size_select', 'appearance_select', 'tick_steps_select', 'style_for_select', 'effect_method_select', 'follow_select') if key == 'design'})


def render_all(destination, dark=True):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    theme.init_fonts()
    with mock.patch.object(theme, 'system_dark', return_value=dark):
        control = offscreen_control_window()
    control.glow_style_select.value = 'flint'
    control._reflect()
    records = []
    for width in (320, 640, 900, 1440, 1920):
        for key in control.pages:
            control._select_page(key)
            control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
            for _ in range(3):
                settle(control.window)
            scroll = control.pages[key]
            clip = scroll.contentView()
            page = scroll.documentView()
            record = measure(control, key)
            maximum = max(0, page.bounds().size.height - clip.bounds().size.height)
            for position, fraction in (('top', 0), ('middle', .5), ('bottom', 1)):
                clip.scrollToPoint_((0, maximum * fraction))
                scroll.reflectScrolledClipView_(clip)
                settle(control.window)
                for density in (1, 2):
                    render(control.window.contentView(), density, destination / f'{key}-{width}-{density}x-{position}.png')
            for density in (1, 2):
                render(page, density, destination / f'{key}-{width}-{density}x-document.png')
            records.append(record)
            print(key, width, 'actual', record['frame'][2], 'column', record['column'], 'overflows', record['overflows'], flush=True)
    (destination / 'measurements.json').write_text(json.dumps(records, indent=2) + '\n')


if __name__ == '__main__':
    import sys
    for dark, name in ((True, 'dark'), (False, 'light')):
        render_all(Path(sys.argv[1]) / name, dark)
