"""Offscreen-only settings inspection; never activates or orders a window."""

import json
import math
import statistics
import time
from pathlib import Path

from mac_app.tests.test_window_geometry import offscreen_control_window, kvm_bridge_app

AppKit = kvm_bridge_app.AppKit


def settle(window):
    from Foundation import NSDate, NSRunLoop
    window.contentView().layoutSubtreeIfNeeded()
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
    window.contentView().layoutSubtreeIfNeeded()
    assert not window.isVisible()


def rect_list(rect):
    return [rect.origin.x, rect.origin.y, rect.size.width, rect.size.height]


def capture(view):
    bounds = view.bounds()
    rep = view.bitmapImageRepForCachingDisplayInRect_(bounds)
    view.cacheDisplayInRect_toBitmapImageRep_(bounds, rep)
    return bytes(rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))


def lit_pixels(view):
    rep = view.bitmapImageRepForCachingDisplayInRect_(view.bounds())
    view.cacheDisplayInRect_toBitmapImageRep_(view.bounds(), rep)
    data = bytes(rep.bitmapData())
    channels = rep.samplesPerPixel()
    # Screens and pointer are neutral. Coloured pixels are the effect itself.
    count = 0
    for y in range(rep.pixelsHigh()):
        for x in range(rep.pixelsWide()):
            offset = y * rep.bytesPerRow() + x * channels
            pixel = data[offset:offset + 3]
            count += max(pixel) - min(pixel) > 35 and max(pixel) > 70
    return count


def tiles(control):
    return {value: rect_list(tile.convertRect_toView_(tile.bounds(), control.crossing_styles_box))
            for row in control.glow_style_select.rows for value, tile, _name in row.tiles}


def view_frames(view, path="content"):
    result = {path: rect_list(view.frame())}
    for index, child in enumerate(view.subviews()):
        name = type(child).__name__.removeprefix("NSKVONotifying_")
        result.update(view_frames(child, f"{path}/{index}:{name}"))
    return result


def tile_measurements(control):
    frames = tiles(control)
    rows = {}
    for frame in frames.values():
        rows.setdefault(round(frame[1] + frame[3], 1), []).append(frame)
    gaps = []
    slack = []
    width = control.crossing_styles_box.bounds().size.width
    for row in rows.values():
        row.sort()
        gaps.extend(second[0] - first[0] - first[2] for first, second in zip(row, row[1:]))
        slack.append(width - row[-1][0] - row[-1][2])
    outside = []
    for choices in control.glow_style_select.rows:
        for value, tile, _name in choices.tiles:
            for child in tile.subviews():
                rect = child.convertRect_toView_(child.bounds(), tile)
                if not AppKit.NSContainsRect(tile.bounds(), rect):
                    outside.append(value)
    return dict(gaps=gaps, right_slack=slack, tile_content_outside=outside,
                tile_widths=[frame[2] for frame in frames.values()])


def panel_overflows(body):
    outside = []

    def inspect(view, panel, path):
        if view.isHidden():
            return
        rect = view.convertRect_toView_(view.bounds(), panel)
        if not AppKit.NSContainsRect(AppKit.NSInsetRect(panel.bounds(), -.5, -.5), rect):
            outside.append(dict(path=path, frame=rect_list(rect), panel=rect_list(panel.bounds())))
        for index, child in enumerate(view.subviews()):
            inspect(child, panel, f"{path}/{index}:{type(child).__name__}")

    for index, panel in enumerate(body.arrangedSubviews()[1:]):
        for child_index, child in enumerate(panel.subviews()):
            inspect(child, panel, f"panel-{index}/{child_index}:{type(child).__name__}")
    return outside


def interaction_measurements(control):
    def walk(view):
        yield view
        for child in view.subviews():
            yield from walk(child)

    scroll = control.pages["design"]
    controls = [view for view in walk(scroll) if isinstance(view, kvm_bridge_app.widgets.Pressable)]
    started = time.perf_counter()
    for _ in range(5):
        for view in controls:
            view.resetCursorRects()
    cursor_ms = (time.perf_counter() - started) * 1000
    clip = scroll.contentView()
    maximum = max(0, scroll.documentView().frame().size.height - clip.bounds().size.height)
    times = []
    for index in range(61):
        started = time.perf_counter()
        clip.scrollToPoint_((0, maximum * index / 60))
        scroll.reflectScrolledClipView_(clip)
        control.window.contentView().layoutSubtreeIfNeeded()
        times.append((time.perf_counter() - started) * 1000)
    return dict(controls=len(controls), cursor_reset_passes=5, cursor_reset_ms=cursor_ms,
                scroll_offsets=61, scroll_median_ms=statistics.median(times), scroll_max_ms=max(times),
                scroll_class=type(scroll).__name__, elasticity=scroll.verticalScrollElasticity(),
                predominant_axis=scroll.usesPredominantAxisScrolling(), scroller_style=scroll.scrollerStyle())


def resize_measurements(control):
    window = control.window
    frames = []
    changes_after_settling = []
    arrange_times = []
    originals = []
    for choices in (*control.glow_style_select.rows, *control.switch_style_select.rows, *control.glow_colour_select.rows):
        grid = choices.grid
        arrange = grid.arrange

        def measured_arrange(width, arrange=arrange):
            started = time.perf_counter()
            try:
                return arrange(width)
            finally:
                arrange_times.append((time.perf_counter() - started) * 1000)

        grid.arrange = measured_arrange
        originals.append((grid, arrange))
    widths = list(range(760, 1901, 20)) + list(range(1900, 759, -20))
    try:
        for index, width in enumerate(widths):
            arrange_start = len(arrange_times)
            pair_before = [pair.orientation() for pair in control.side_by_side]
            started = time.perf_counter()
            window.setFrame_display_(((100000 + index, 100000 + index), (width, 800 + index % 7)), False)
            callback_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            window.contentView().layoutSubtreeIfNeeded()
            layout_ms = (time.perf_counter() - started) * 1000
            first = view_frames(window.contentView())
            settle(window)
            later = view_frames(window.contentView())
            changed = [dict(path=path, before=frame, after=later.get(path))
                       for path, frame in first.items() if later.get(path) != frame]
            if changed:
                changes_after_settling.append(dict(index=index, views=changed))
            frames.append(dict(width=width, milliseconds=callback_ms + layout_ms,
                               callback_ms=callback_ms, layout_ms=layout_ms,
                               arrange_ms=sum(arrange_times[arrange_start:]),
                               columns=control.glow_style_select.rows[0].columns, wide=control.wide,
                               pair_flips=[index for index, pair in enumerate(control.side_by_side)
                                           if pair.orientation() != pair_before[index]]))
    finally:
        for grid, arrange in originals:
            grid.arrange = arrange
    origins = []
    for index in range(20):
        before = view_frames(window.contentView())
        started = time.perf_counter()
        window.setFrameOrigin_((100000 + index * 10, 100000))
        window.contentView().layoutSubtreeIfNeeded()
        origins.append((time.perf_counter() - started) * 1000)
        assert view_frames(window.contentView()) == before
    times = sorted(frame["milliseconds"] for frame in frames)
    return dict(frames=frames, median_ms=statistics.median(times), p95_ms=times[math.ceil(.95 * len(times)) - 1],
                max_ms=max(times), unsettled_frames=changes_after_settling,
                move_ms=origins, move_max_ms=max(origins))


def render_all(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    control = offscreen_control_window()
    control._select_page("design")
    control.glow_style_select.value = "beam"
    control._reflect()
    window = control.window
    report = []
    minimum = window.minSize()
    for width, height in [(760, 900), (900, 900), (1100, 900), (1400, 900), (1900, 900),
                          (int(minimum.width), int(minimum.height))]:
        window.setFrame_display_(((100000, 100000), (width, height)), False)
        settle(window)
        scroll = control.pages["design"]
        clip = scroll.contentView()
        page = scroll.documentView()
        body = page.subviews()[0]
        maximum = max(0, page.frame().size.height - clip.bounds().size.height)
        counts = {}
        for row in control.glow_style_select.rows:
            for (value, _tile, _name), preview in zip(row.tiles, row.previews):
                counts[value] = lit_pixels(preview)
        for position, fraction in [("top", 0), ("middle", .5), ("bottom", 1)]:
            clip.scrollToPoint_((0, maximum * fraction))
            scroll.reflectScrolledClipView_(clip)
            settle(window)
            path = destination / f"design-{width}x{height}-{position}.png"
            path.write_bytes(capture(window.contentView()))
            content = window.contentView()
            footer = control.message_label.view.convertRect_toView_(
                control.message_label.view.bounds(), content)
            viewport = scroll.convertRect_toView_(scroll.bounds(), content)
            last = body.arrangedSubviews()[-1]
            last_rect = last.convertRect_toView_(last.bounds(), page)
            fields = {name: rect_list(view.convertRect_toView_(view.bounds(), view.superview()))
                      for name, view in (("length-row", control.length_row), ("size-row", control.size_row),
                                        ("length-options", control.length_select.view),
                                        ("size-options", control.size_select.view))}
            report.append(dict(path=str(path), window=rect_list(window.frame()),
                               position=position, scroll=rect_list(scroll.frame()),
                               document=rect_list(page.frame()), clip=rect_list(clip.bounds()),
                               grid=rect_list(control.crossing_styles_box.frame()),
                               tiles=tiles(control), geometry=tile_measurements(control), lit_pixels=counts,
                               fields=fields, footer=rect_list(footer),
                               panel_content_outside=panel_overflows(body),
                               viewport_footer_gap=viewport.origin.y - footer.origin.y - footer.size.height,
                               last_content_bottom_clearance=page.frame().size.height - last_rect.origin.y - last_rect.size.height,
                               modules=[rect_list(module.convertRect_toView_(module.bounds(), body))
                                        for module in body.arrangedSubviews()]))
    path = destination / "measurements.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    (destination / "resize-measurements.json").write_text(
        json.dumps(resize_measurements(control), indent=2) + "\n")
    (destination / "scroll-measurements.json").write_text(
        json.dumps(interaction_measurements(control), indent=2) + "\n")
    return report


if __name__ == "__main__":
    import sys
    records = render_all(sys.argv[1])
    for record in records:
        print(record["path"], "document", record["document"], "clip", record["clip"],
              "Beam lit", record["lit_pixels"]["beam"], flush=True)
