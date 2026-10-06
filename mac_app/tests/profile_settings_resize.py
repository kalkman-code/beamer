"""Unordered resize profiling, with optional isolated text-layout experiments."""
import cProfile
import json
import sys
import time
import os
import subprocess
from pathlib import Path

from mac_app.tests.design_offscreen import offscreen_control_window, resize_measurements, settle
from mac_app.tests.test_settings_layout_acceptance import walk
import AppKit


def main():
    if '--fonts' in sys.argv:
        from mac_app.tests.design_offscreen import kvm_bridge_app
        kvm_bridge_app.theme.init_fonts()
    control = offscreen_control_window()
    control._select_page('design')
    settle(control.window)
    if '--empty-tiles' in sys.argv:
        for choices in (control.glow_style_select, control.switch_style_select, control.glow_colour_select):
            for row in choices.rows:
                for cell in row.grid.cells:
                    for child in list(cell.subviews()):
                        child.removeFromSuperview()
        settle(control.window)
    if '--no-tiles' in sys.argv:
        for choices in (control.glow_style_select, control.switch_style_select, control.glow_colour_select):
            for row in choices.rows:
                row.view.removeFromSuperview()
        settle(control.window)
    if '--single-line' in sys.argv:
        for view in walk(control.pages['design']):
            if isinstance(view, AppKit.NSTextField):
                view.cell().setWraps_(False)
                view.cell().setUsesSingleLineMode_(True)
        settle(control.window)
    profiler = cProfile.Profile()
    lines, previous = {}, {}

    def trace(frame, event, arg):
        if frame.f_code.co_name != 'windowDidResize_':
            return None
        now = time.perf_counter()
        held = previous.get(id(frame))
        if held:
            line, started = held
            lines.setdefault(line, []).append((now - started) * 1000)
        if event == 'return':
            previous.pop(id(frame), None)
        else:
            previous[id(frame)] = (frame.f_lineno, now)
        return trace

    if '--trace-lines' in sys.argv:
        sys.settrace(trace)
    profiler.enable()
    sampler = None
    if '--sample-native' in sys.argv:
        sampler = subprocess.Popen(['sample', str(os.getpid()), '5', '1', '-file', str(Path(sys.argv[1]).with_suffix('.sample'))],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if '--hot-native' in sys.argv:
        for index in range(600):
            width = 760 + (index % 40 if index % 80 < 40 else 79 - index % 80) * 10
            control.window.setFrame_display_(((100000, 100000), (width, 800)), False)
            control.window.contentView().layoutSubtreeIfNeeded()
        result = resize_measurements(control)
    else:
        for _ in range(5 if sampler else 1):
            result = resize_measurements(control)
    if sampler:
        sampler.wait()
    profiler.disable()
    sys.settrace(None)
    destination = Path(sys.argv[1])
    destination.with_suffix('.json').write_text(json.dumps(result, indent=2) + '\n')
    profiler.dump_stats(str(destination.with_suffix('.prof')))
    if lines:
        print({line: {'total_ms': sum(values), 'max_ms': max(values), 'calls': len(values)} for line, values in lines.items()})
    print({key: result[key] for key in ('median_ms', 'p95_ms', 'max_ms')})


if __name__ == '__main__':
    main()
