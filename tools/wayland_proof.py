"""The Wayland back ends against a real GNOME Shell and its portals: what the fakes in win_app/tests
cannot prove.

Run by tools/wayland_proof.sh, which starts a headless GNOME Shell on its own D-Bus session. The
observer (tools/wayland_observer.py) is a full-screen app that logs what reaches it. This machine's
own mouse and keyboard are played through mutter's own remote desktop D-Bus (`Hand`), which a
headless shell lets any client use; the desktop's consent dialogs are answered with their own
keyboard shortcuts through it, as a person would. GNOME keeps a dialog that opens over a focused
window behind it, unfocused, so the observer is closed while a dialog is up and opened again
after.

Each check prints PASS or FAIL, and NOTE for what is only reported; the exit status is the number
of failures. Proved here, through capture_portal: the barrier on a zone, a push held there and
crossing once the sender says so, keys, buttons, the wheel and motion arriving while input is away
and reaching no app, and every way home letting the pointer go where it should: the sender's
landing, a move back into the screen, a pause, a key at the edge, stop(), a killed process and a
stalled thread. Through inject_portal: the remembered permission, the pointer placed and moved,
buttons, keys and text off the layout, the wheel, everything let go, and a second start asking
nothing. Through clipboard_portal: another app pastes Beamer's text and image, and Beamer reads
another app's copy and not the echo of its own."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "win_app"), str(ROOT)]

WORK = Path(os.environ["BEAMER_PROOF_DIR"])
OBSERVED = WORK / "observed.jsonl"
COMMANDS = WORK / "commands"
# evdev codes
ESC, A, S, ONE, LEFTSHIFT, LEFTCTRL, LEFTALT, C, I = 1, 30, 31, 2, 42, 29, 56, 46, 23
BTN_LEFT = 0x110
CAPTURE_ANSWER = [(LEFTALT, I), (LEFTALT, S)]          # Allow Remote Input Capturing, Share
REMOTE_ANSWER = [(LEFTALT, I), (LEFTALT, C), (LEFTALT, S)]  # Interaction, Clipboard, Share

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"), flush=True)
    if not ok:
        failures.append(name)


def note(text):
    print("NOTE " + text, flush=True)


def wait(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


class Hand:
    """This machine's own keyboard and mouse: mutter's remote desktop session, relative motion."""

    def __init__(self):
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection

        self._new = new_method_call
        self.conn = open_dbus_connection(bus="SESSION")
        manager = DBusAddress("/org/gnome/Mutter/RemoteDesktop", "org.gnome.Mutter.RemoteDesktop",
                              "org.gnome.Mutter.RemoteDesktop")
        path = self.conn.send_and_get_reply(new_method_call(manager, "CreateSession")).body[0]
        self.session = DBusAddress(path, "org.gnome.Mutter.RemoteDesktop", "org.gnome.Mutter.RemoteDesktop.Session")
        self._call("Start")
        self._lock = threading.Lock()

    def _call(self, method, signature=None, body=()):
        reply = self.conn.send_and_get_reply(self._new(self.session, method, signature, body))
        if reply.header.message_type.name == "error":
            raise RuntimeError(f"{method}: {reply.body}")

    def key(self, code, down):
        with self._lock:
            self._call("NotifyKeyboardKeycode", "ub", (code, down))

    def tap(self, *codes):
        for code in codes:
            self.key(code, True)
        for code in reversed(codes):
            self.key(code, False)

    def move(self, dx, dy):
        with self._lock:
            self._call("NotifyPointerMotionRelative", "dd", (float(dx), float(dy)))

    def to(self, x, y):
        """Into the top left corner, then out to (x, y), one axis at a time in small steps: mutter
        dropped one axis of a single large diagonal move (01-10-2026)."""
        for dx, dy in ((-4000, 0), (0, -4000)):
            self.move(dx, dy)
        for axis, distance in ((0, x), (1, y)):
            while distance > 0:
                step = min(100, distance)
                self.move(step if axis == 0 else 0, step if axis == 1 else 0)
                distance -= step
        time.sleep(0.1)

    def button(self, code, down):
        with self._lock:
            self._call("NotifyPointerButton", "ib", (code, down))

    def wheel(self, steps):
        with self._lock:
            self._call("NotifyPointerAxisDiscrete", "ui", (0, steps))

    def answer_later(self, chords, delay=2.5, gap=0.8):
        def go():
            time.sleep(delay)
            for chord in chords:
                self.tap(*chord)
                time.sleep(gap)

        thread = threading.Thread(target=go, daemon=True)
        thread.start()
        return thread


class Observer:
    """The full-screen app and what reached it, read from its log."""

    def __init__(self):
        self._offset = 0
        self._process = None

    def launch(self):
        self.mark()
        self._process = subprocess.Popen(
            [os.environ.get("OBSERVER_PYTHON", "/usr/bin/python3"), str(Path(__file__).with_name("wayland_observer.py")),
             str(OBSERVED), str(COMMANDS)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ready = wait(lambda: self.seen("ready"), timeout=15)
        time.sleep(0.5)
        self.mark()
        return ready

    def close(self):
        if self._process is None:
            return
        self.command("quit")
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._process = None
        time.sleep(0.3)

    def mark(self):
        self._offset = OBSERVED.stat().st_size if OBSERVED.exists() else 0

    def seen(self, kind=None):
        if not OBSERVED.exists():
            return []
        with open(OBSERVED) as handle:
            handle.seek(self._offset)
            found = [json.loads(line) for line in handle if line.strip()]
        return [item for item in found if kind is None or item["kind"] == kind]

    def keys(self):
        return [(item["name"], item["down"]) for item in self.seen("key")]

    def last_motion(self):
        motions = self.seen("motion")
        return (round(motions[-1]["x"]), round(motions[-1]["y"])) if motions else None

    def command(self, line):
        with open(COMMANDS, "a") as handle:
            handle.write(line + "\n")


class Sender:
    """What capture_portal's callbacks reach, with the sender's crossing as a threshold of pushed
    pixels, as the zone model's resistance is."""

    def __init__(self, threshold=60):
        self.lock = threading.Lock()
        self.threshold = threshold
        self.away = False
        self.pushed = 0
        self.keys, self.mice, self.motions = [], [], []
        self.block_keys = 0.0

    def on_key(self, name, down, vk, us):
        if self.block_keys:
            time.sleep(self.block_keys)
        with self.lock:
            self.keys.append((name, down))
        return True

    def on_mouse(self, message, x, y, data):
        with self.lock:
            self.mice.append((message, data))
        return True

    def on_motion(self, dx, dy):
        with self.lock:
            self.motions.append((dx, dy))
            if not self.away:
                self.pushed += dx
                if self.pushed >= self.threshold:
                    self.away = True

    def take(self):
        with self.lock:
            found = (self.keys, self.mice, self.motions)
            self.keys, self.mice, self.motions = [], [], []
        return found


def start_capture(hand, sender, observer):
    import capture_portal
    from core import return_edge

    observer.close()
    hooks = capture_portal.Hooks(sender.on_key, sender.on_mouse, sender.on_motion)
    hooks.grab_while(lambda: sender.away)
    hooks.barriers_from(lambda: [return_edge.ReturnEdge("right")])
    homes = []
    hooks.on_home = lambda: homes.append(True) or setattr(sender, "away", False)
    failures_seen = []
    hooks.on_failure = failures_seen.append
    hand.answer_later(CAPTURE_ANSWER)
    hooks.start()
    ok = wait(lambda: hooks._session is not None and hooks._session.enabled, timeout=15)
    observer.launch()
    return hooks, ok, homes, failures_seen


def push_to_edge(hand, y=400, pushes=12, step=10):
    hand.to(1270, y)
    for _ in range(pushes):
        hand.move(step, 0)
        time.sleep(0.02)


def proof_capture(hand, observer):
    import capture_portal
    import desktop_portal

    sender = Sender()
    hooks, ok, homes, seen_failures = start_capture(hand, sender, observer)
    check("the capture session starts once the desktop is answered and its barrier is accepted",
          ok and hooks._walls == {1: "right"}, f"enabled={ok} walls={getattr(hooks, '_walls', None)} failures={seen_failures}")
    if not ok:
        hooks.stop()
        return

    observer.mark()
    push_to_edge(hand)
    crossed = wait(lambda: sender.away and hooks._away)
    check("a push into the barrier is held and crosses once the sender says so", crossed and hooks.holding,
          f"away={sender.away} pushed={sender.pushed} holding={hooks.holding}")
    check("the pointer is held on the last pixel of the edge", desktop_portal.position() == (1279, 400),
          str(desktop_portal.position()))
    sender.take()

    hand.tap(A)
    hand.tap(LEFTSHIFT, A)
    hand.tap(LEFTCTRL)
    hand.button(BTN_LEFT, True)
    hand.button(BTN_LEFT, False)
    hand.wheel(1)
    hand.move(15, -7)
    time.sleep(0.4)
    keys, mice, motions = sender.take()
    check("keys while away are named as the layout types them", keys == [
        ("a", True), ("a", False), ("shift", True), ("A", True), ("A", False), ("shift", False), ("cmd", True), ("cmd", False)], str(keys))
    left_down, left_up = capture_portal._BUTTONS[1][:2]
    wheel = [m for m in mice if m[0] == capture_portal.WM_MOUSEWHEEL]
    check("buttons and the wheel while away arrive as the mouse messages",
          [m for m in mice if m[0] != capture_portal.WM_MOUSEWHEEL] == [(left_down, 0), (left_up, 0)]
          and wheel == [(capture_portal.WM_MOUSEWHEEL, capture_portal._wheel_data(-capture_portal.WHEEL_DELTA))], str(mice))
    check("motion while away arrives whole", (sum(m[0] for m in motions), sum(m[1] for m in motions)) == (15, -7), str(motions))
    check("nothing captured reached an app", observer.seen("key") == [] and observer.seen("button") == []
          and observer.seen("scroll") == [], str(observer.seen()[:6]))

    desktop_portal.set_cursor_position(600, 400)
    sender.away = False
    released = wait(lambda: not hooks.holding)
    hand.move(1, 0)
    check("coming home lets the pointer go where the sender lands it", released and wait(lambda: observer.last_motion() == (601, 400)),
          f"released={released} last={observer.last_motion()}")

    # A move back, a pause and a key, each at the edge without crossing.
    sender.threshold = 10 ** 6
    for name, act, expect in (
        ("a move back into the screen lets go where it ends", lambda: hand.move(-20, 0), (1258, 400)),
        ("a pause in the push lets go at the edge", lambda: time.sleep(capture_portal.IDLE_SECONDS + 0.3), (1278, 400)),
        ("a key at the edge lets go and reaches nobody", lambda: hand.tap(S), (1278, 400)),
    ):
        observer.mark()
        sender.take()
        push_to_edge(hand, pushes=3)
        held = wait(lambda: hooks.holding)
        act()
        let_go = wait(lambda: not hooks.holding)
        hand.move(-1, 0)  # one pixel back in, where the app sees where it was let go
        landed = wait(lambda: observer.last_motion() == expect)
        keys = sender.take()[0]
        check(name, held and let_go and landed and keys == [] and observer.keys() == [],
              f"held={held} let_go={let_go} last={observer.last_motion()} keys={keys} app_keys={observer.keys()}")

    # A drag into the barrier: whether GNOME captures with a button held.
    observer.mark()
    hand.to(1270, 300)
    hand.button(BTN_LEFT, True)
    for _ in range(4):
        hand.move(10, 0)
        time.sleep(0.02)
    dragged = wait(lambda: hooks.holding, timeout=0.5)
    hand.button(BTN_LEFT, False)
    wait(lambda: not hooks.holding, timeout=2)
    note(f"a drag into the barrier {'is' if dragged else 'is not'} captured by GNOME")

    sender.threshold = 60
    sender.pushed = 0
    push_to_edge(hand)
    away = wait(lambda: sender.away and hooks._away)
    hooks.stop()
    observer.mark()
    hand.tap(A)
    check("stop() while input is away gives it back", away and wait(lambda: ("a", True) in observer.keys()),
          f"away={away} app_keys={observer.keys()}")
    sender.away = False

    # The watchdog: a callback that hangs while the desktop holds input.
    import capture_portal as module

    sender = Sender()
    hooks, ok, homes, seen_failures = start_capture(hand, sender, observer)
    push_to_edge(hand)
    away = ok and wait(lambda: sender.away and hooks._away)
    sender.block_keys = 30.0
    hand.tap(A)  # the capture thread hangs in on_key
    observer.mark()
    time.sleep(module.STALL_SECONDS + 1.0)
    hand.tap(S)
    freed = wait(lambda: ("s", True) in observer.keys(), timeout=3)
    check("a capture thread stalled while input is away has its connection ended and input comes back",
          away and freed, f"away={away} app_keys={observer.keys()}")
    sender.block_keys = 0.0


def capture_child():
    """Capture until killed; says 'away' once input is away."""
    import capture_portal
    from core import return_edge

    sender = Sender()
    hooks = capture_portal.Hooks(sender.on_key, sender.on_mouse, sender.on_motion)
    hooks.grab_while(lambda: sender.away)
    hooks.barriers_from(lambda: [return_edge.ReturnEdge("right")])
    hooks.start()
    enabled = wait(lambda: hooks._session is not None and hooks._session.enabled, timeout=15)
    print("enabled" if enabled else "not enabled", flush=True)
    away = enabled and wait(lambda: sender.away and hooks._away, timeout=15)
    print("away" if away else "not away", flush=True)
    time.sleep(60)


def proof_killed_process_lets_go(hand, observer):
    observer.close()
    child = subprocess.Popen([sys.executable, __file__, "capture-child"], stdout=subprocess.PIPE, text=True)
    try:
        hand.answer_later(CAPTURE_ANSWER)
        enabled = child.stdout.readline().strip() == "enabled"
        observer.launch()
        push_to_edge(hand)
        away = child.stdout.readline().strip() == "away"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        observer.mark()
        time.sleep(0.5)
        hand.tap(A)
        check("a killed process holding input away gives it back", enabled and away and wait(lambda: ("a", True) in observer.keys()),
              f"enabled={enabled} away={away} app_keys={observer.keys()}")
    finally:
        if child.poll() is None:
            child.kill()


def proof_injection(hand, observer):
    import desktop_portal
    import inject_portal
    import portal

    portal.TokenStore("remote-desktop").write("")
    observer.close()
    problems = []
    inject_portal.on_problem = problems.append
    hand.answer_later(REMOTE_ANSWER)
    remote = inject_portal.remote()
    remote.start()
    up = wait(lambda: remote.ready.is_set() and remote.layout is not None and remote._input_ready()
              and remote._with(inject_portal.CAP_POINTER_ABSOLUTE) is not None, timeout=15)
    check("remote control starts once the desktop is answered, with a keyboard, its layout and an absolute pointer",
          up and not problems, f"ready={remote.ready.is_set()} problems={problems}")
    if not up:
        return
    check("the desktop's permission is remembered", portal.TokenStore("remote-desktop").read() != "")
    observer.launch()

    observer.mark()
    desktop_portal.set_cursor_position(100, 100)
    inject_portal.inject_mouse_move(10, 5)
    check("the pointer is placed and moved where Beamer says", wait(lambda: observer.last_motion() == (110, 105)),
          str(observer.last_motion()))
    inject_portal.inject_mouse_button("left", True)
    inject_portal.inject_mouse_button("left", False)
    buttons = lambda: [(b["button"], b["down"], round(b["x"]), round(b["y"])) for b in observer.seen("button")]  # noqa: E731
    check("a button presses and lets go where the pointer is", wait(lambda: buttons() == [(1, True, 110, 105), (1, False, 110, 105)]),
          str(buttons()))

    observer.mark()
    for name in ("a", "A", "!", "return"):
        inject_portal.inject_key(name, True)
        inject_portal.inject_key(name, False)
    inject_portal.inject_text("hi!")
    expect = [("a", True), ("a", False), ("Shift_L", True), ("A", True), ("A", False), ("Shift_L", False),
              ("Shift_L", True), ("exclam", True), ("exclam", False), ("Shift_L", False), ("Return", True), ("Return", False),
              ("h", True), ("h", False), ("i", True), ("i", False), ("Shift_L", True), ("exclam", True), ("Shift_L", False),
              ("1", False)]  # let go after Shift, so the app names it by its unshifted keysym
    typed = wait(lambda: len(observer.keys()) >= len(expect), timeout=3)
    keys = [key for key in observer.keys()]
    check("keys and text type as the layout has them", typed and keys == expect, str(keys))

    observer.mark()
    inject_portal.inject_key("é", True)
    inject_portal.inject_key("é", False)
    time.sleep(0.4)
    check("a character the layout has no key for is not typed", observer.keys() == [], str(observer.keys()))

    observer.mark()
    inject_portal.inject_key("ctrl", True)
    inject_portal.inject_key("c", True)
    inject_portal.inject_key("c", False)
    inject_portal.inject_key("ctrl", False)
    chord = wait(lambda: len(observer.keys()) >= 4)
    ctrl_c = [item for item in observer.seen("key") if item["name"] == "c" and item["down"]]
    check("a chord reaches the app with its modifier held", chord and ctrl_c and ctrl_c[0]["state"] & 4,
          str(observer.seen("key")))

    observer.mark()
    inject_portal.inject_scroll(1, 0, "line")
    check("the wheel scrolls away from the hand", wait(lambda: any(s["dy"] < 0 for s in observer.seen("scroll"))),
          str(observer.seen("scroll")))

    observer.mark()
    inject_portal.inject_key("shift", True)
    inject_portal.inject_mouse_button("right", True)
    inject_portal.release_all()
    released = wait(lambda: ("Shift_L", False) in observer.keys() and any(
        b["button"] == 3 and not b["down"] for b in observer.seen("button")))
    check("release_all lets go of every key and button held for the peer", released,
          f"{observer.keys()} {observer.seen('button')}")

    proof_clipboard(observer)

    inject_portal.stop()
    inject_portal._remote = None
    started = time.monotonic()
    remote = inject_portal.remote()
    remote.start()
    restored = wait(lambda: remote.ready.is_set(), timeout=5)
    check("a second start asks nothing: the permission is restored", restored and time.monotonic() - started < 2.0,
          f"ready={restored} after {time.monotonic() - started:.2f} s")
    inject_portal.stop()


def png_bytes():
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(4, 3, QImage.Format.Format_RGB32)
    image.fill(QColor(20, 120, 220))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def proof_clipboard(observer):
    import clipboard_portal

    observer.mark()
    check("Beamer's text is offered as the clipboard", clipboard_portal.set_contents("from beamer", None))
    # The compositor announces a new offer to an app a moment after it is made: a paste at once
    # can find the old one, so it is tried again, as a person would.
    found = []
    for _ in range(3):
        observer.mark()
        observer.command("paste")
        wait(lambda: observer.seen("paste"), timeout=4)
        found = [item.get("text") for item in observer.seen("paste")]
        if found == ["from beamer"]:
            break
        time.sleep(0.3)
    check("another app pastes Beamer's text", found == ["from beamer"], str(observer.seen()))
    check("Beamer's own copy is not read back as a change", clipboard_portal.changed_contents() == (None, None))

    stamp = clipboard_portal.change_stamp()
    observer.command("copy:from the desktop")
    moved = wait(lambda: clipboard_portal.change_stamp() != stamp, timeout=4)
    contents = clipboard_portal.changed_contents()
    check("Beamer reads another app's copy once", moved and contents == ("from the desktop", None)
          and clipboard_portal.changed_contents() == (None, None), f"moved={moved} {contents}")

    png = png_bytes()
    check("Beamer's image is offered as the clipboard", clipboard_portal.set_contents(None, png))
    pasted = subprocess.run(["wl-paste", "--no-newline", "--type", "image/png"], capture_output=True, timeout=10)
    check("another app pastes Beamer's image", pasted.stdout == png,
          f"{len(pasted.stdout)} bytes, sha {hashlib.sha256(pasted.stdout).hexdigest()[:12]}")
    copier = subprocess.Popen(["wl-copy", "--type", "image/png"], stdin=subprocess.PIPE)
    copier.communicate(png, timeout=10)
    wait(lambda: clipboard_portal.change_stamp() is not None, timeout=1)
    got = None
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and got is None:
        text, image = clipboard_portal.changed_contents()
        got = image
        time.sleep(0.1)
    check("Beamer reads another app's image", got == png, f"{None if got is None else len(got)} bytes")


def main():
    import logging

    logging.basicConfig(level=logging.INFO, format="LOG %(name)s %(levelname)s %(message)s")
    hand = Hand()
    hand.tap(ESC)  # out of the Overview the headless shell starts in
    # The portal and its GNOME back end start on first use, slowly: woken here, so the first dialog
    # opens as soon as it is asked for.
    subprocess.run(["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop", "--object-path",
                    "/org/freedesktop/portal/desktop", "--method", "org.freedesktop.portal.Settings.ReadAll", "[]"],
                   capture_output=True, timeout=30)
    time.sleep(1.0)
    observer = Observer()
    try:
        proof_injection(hand, observer)
        proof_capture(hand, observer)
        proof_killed_process_lets_go(hand, observer)
    finally:
        observer.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "capture-child":
        capture_child()
    else:
        main()
        print(f"{len(failures)} failed" if failures else "all passed", flush=True)
        os._exit(min(len(failures), 100))
