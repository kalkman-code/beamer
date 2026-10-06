"""The X11 back ends against a real X server: what the fakes in win_app/tests cannot prove.

Run by tools/x11_proof.sh, which starts Xvfb and sets DISPLAY; on a real desktop it can be run
directly (`python3 tools/x11_proof.py`), but it grabs the keyboard and mouse for a moment and
types into whatever has focus, so run it from a terminal with nothing else open.

Each check prints PASS or FAIL; the exit status is the number of failures. Proved here:
injection through inject_x11 read back through capture_x11's listener (motion, every modifier,
letters, text off the layout, the wheel, every button), the listener dropping XTEST by default,
the grab (raw events still arrive, a second client's grab is refused, other clients see nothing,
and stop() and a killed process both let go), desktop_x11's geometry and warp, and the clipboard
owned for real: another app pastes what Beamer wrote, Beamer sees another app's copy, and not
the echo of its own."""

import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "win_app"), str(ROOT)]

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f": {detail}"), flush=True)
    if not ok:
        failures.append(name)


class Seen:
    def __init__(self):
        self.lock = threading.Lock()
        self.keys, self.mice, self.motions = [], [], []
        # What on_key answers: False hands a key from inside the grab back to this machine.
        self.key_answer = None

    def on_key(self, name, down, vk, us):
        with self.lock:
            self.keys.append((name, down, vk, us))
        return self.key_answer

    def on_mouse(self, message, x, y, data):
        with self.lock:
            self.mice.append((message, data))

    def on_motion(self, dx, dy):
        with self.lock:
            self.motions.append((dx, dy))

    def take(self, settle=0.3):
        time.sleep(settle)
        with self.lock:
            found = (self.keys, self.mice, self.motions)
            self.keys, self.mice, self.motions = [], [], []
        return found


def wait(condition, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def other_client_grab():
    """Whether a second client can grab the master keyboard now; it lets go at once if it can."""
    from Xlib import X, display
    from Xlib.ext import xinput

    other = display.Display()
    try:
        opcode = other.display.get_extension_major("XInputExtension")
        keyboard = next(info.deviceid for info in other.xinput_query_device(xinput.AllMasterDevices).devices
                        if info.use == xinput.MasterKeyboard)
        reply = xinput.XIGrabDevice(display=other.display, opcode=opcode, deviceid=keyboard, grab_window=other.screen().root,
                                    time=X.CurrentTime, cursor=X.NONE, grab_mode=xinput.GrabModeAsync,
                                    paired_device_mode=xinput.GrabModeAsync, owner_events=False, mask=0)
        if reply.status == 0:
            other.xinput_ungrab_device(keyboard, X.CurrentTime)
            other.sync()
        return reply.status == 0
    finally:
        other.close()


class Listener:
    """Another client listening for ordinary XI2 key presses on the root, as an app would: it sees
    them while nothing is grabbed, and nothing while Beamer holds the keyboard."""

    def __init__(self):
        from Xlib import display
        from Xlib.ext import xinput

        self.display = display.Display()
        self.opcode = self.display.display.get_extension_major("XInputExtension")
        xinput.XIQueryVersion(display=self.display.display, opcode=self.opcode, major_version=2, minor_version=2)
        self.display.screen().root.xinput_select_events([(xinput.AllMasterDevices, xinput.KeyPressMask)])
        self.display.sync()

    def presses(self):
        time.sleep(0.3)
        count = 0
        while self.display.pending_events():
            event = self.display.next_event()
            if event.type == 35 and event.extension == self.opcode and event.evtype == 2:
                count += 1
        return count


def proof_injection_and_capture():
    import capture_x11
    import desktop_x11
    import inject_x11

    seen = Seen()
    hooks = capture_x11.Hooks(seen.on_key, seen.on_mouse, seen.on_motion)
    away = [False]
    hooks.grab_while(lambda: away[0])
    hooks.start()
    try:
        seen.take()
        inject_x11.inject_mouse_move(5, 0)
        inject_x11.inject_key("a", True)
        inject_x11.inject_key("a", False)
        keys, mice, motions = seen.take()
        check("the listener drops this machine's own injection", not keys and not mice and not motions, (keys, mice, motions))

        hooks.drop_injected = False
        inject_x11.inject_mouse_move(7, -3)
        keys, mice, motions = seen.take()
        check("relative motion read back as the mouse's counts", motions == [(7, -3)], motions)
        check("motion moves the pin", (capture_x11.WM_MOUSEMOVE, 0) in mice, mice)

        inject_x11.move_to(400, 300)
        seen.take()
        check("an absolute move lands where it was sent", desktop_x11.cursor_position() == (400, 300), desktop_x11.cursor_position())
        desktop_x11.set_cursor_position(123, 45)
        _keys, _mice, motions = seen.take()
        check("a warp moves the pointer", desktop_x11.cursor_position() == (123, 45), desktop_x11.cursor_position())
        check("a warp makes no motion", motions == [], motions)
        monitors = desktop_x11.monitors()
        check("RandR reports the screen", len(monitors) == 1 and (monitors[0].width, monitors[0].height) == (1920, 1080), monitors)

        expected = {"shift": "shift", "shift_r": "shift_r", "ctrl": "cmd", "ctrl_r": "cmd_r",
                    "alt": "alt", "alt_r": "alt_r", "cmd": "ctrl", "cmd_r": "ctrl_r"}
        for pressed, named in expected.items():
            inject_x11.inject_key(pressed, True)
            inject_x11.inject_key(pressed, False)
            keys, _, _ = seen.take(0.15)
            check(f"modifier {pressed} reads back as {named}", [(k[0], k[1]) for k in keys] == [(named, True), (named, False)], keys)

        inject_x11.inject_key("q", True, us="q")
        inject_x11.inject_key("q", False, us="q")
        keys, _, _ = seen.take()
        check("a letter reads back with its code and US place", keys == [("q", True, 16, "q"), ("q", False, 16, "q")], keys)

        for message_dy, message_dx, expect in ((1, 0, ("wheel", 1.0, 0.0)), (-1, 0, ("wheel", -1.0, 0.0)),
                                              (0, 1, ("wheel", 0.0, 1.0)), (0, -1, ("wheel", 0.0, -1.0))):
            inject_x11.inject_scroll(message_dy, message_dx, "line")
            _, mice, _ = seen.take(0.15)
            events = [capture_x11.mouse_event(message, data) for message, data in mice]
            check(f"scroll {message_dy},{message_dx} reads back as {expect}", events == [expect], events)
        inject_x11.inject_scroll(30, 0, "pixel")
        inject_x11.inject_scroll(30, 0, "pixel")
        _, mice, _ = seen.take()
        check("pixel scroll carries its remainder into one click", [capture_x11.mouse_event(*m) for m in mice] == [("wheel", 1.0, 0.0)], mice)

        for button in ("left", "middle", "right", "back", "forward"):
            inject_x11.inject_mouse_button(button, True)
            inject_x11.inject_mouse_button(button, False)
            _, mice, _ = seen.take(0.15)
            events = [capture_x11.mouse_event(message, data) for message, data in mice]
            check(f"button {button} reads back", events == [("button", button, True), ("button", button, False)], events)

        # The grab.
        listener = Listener()
        inject_x11.inject_key("w", True)
        inject_x11.inject_key("w", False)
        check("another app sees keys while nothing is grabbed", listener.presses() == 1)
        seen.take()
        away[0] = True
        check("input away: the capture grabs", wait(lambda: hooks.grabbed))
        check("a second client's grab is refused", not other_client_grab())
        inject_x11.inject_key("shift", True)
        inject_x11.inject_key("A", True)
        inject_x11.inject_key("A", False)
        inject_x11.inject_key("shift", False)
        inject_x11.inject_text("é")
        inject_x11.inject_mouse_move(4, 4)
        keys, mice, motions = seen.take(0.5)
        check("while grabbed, keys arrive with the state at the moment of the key",
              [(k[0], k[1]) for k in keys] == [("shift", True), ("A", True), ("A", False), ("shift", False), ("é", True), ("é", False)], keys)
        check("while grabbed, raw motion still arrives", motions == [(4, 4)], motions)
        check("while grabbed, other apps see no keys", listener.presses() == 0)
        away[0] = False
        check("input home: the capture lets go", wait(lambda: not hooks.grabbed))
        check("after letting go, another client can grab", other_client_grab())
        inject_x11.inject_key("w", True)
        inject_x11.inject_key("w", False)
        check("after letting go, other apps see keys again", listener.presses() == 1)

        proof_layout(hooks, seen, listener, away)

        away[0] = True
        wait(lambda: hooks.grabbed)
        hooks.stop()
        check("stop() lets go", other_client_grab())
        inject_x11.release_all()
    finally:
        hooks.stop()


def fake(keycode, down=None):
    """A key through XTest directly, for keys the injector never names (the keypad, Num Lock)."""
    from Xlib import X, display
    from Xlib.ext import xtest

    other = display.Display()
    try:
        for press in ((True, False) if down is None else (down,)):
            xtest.fake_input(other, X.KeyPress if press else X.KeyRelease, keycode)
        other.sync()
    finally:
        other.close()


def proof_layout(hooks, seen, listener, away):
    """Keys the layout decides, read back through XKB, and repeats and handed-back keys as another
    app sees them."""
    import inject_x11
    from Xlib import XK, display

    names = lambda keys: [(key[0], key[1]) for key in keys]  # noqa: E731
    away[0] = True
    wait(lambda: hooks.grabbed)
    seen.take()

    fake(87)
    keys, _, _ = seen.take()
    check("the keypad with Num Lock off moves (end)", names(keys) == [("end", True), ("end", False)], keys)
    fake(77)
    fake(87)
    keys, _, _ = seen.take()
    check("the keypad with Num Lock on types (1)", [k for k in names(keys) if k[0] != "num_lock"] == [("1", True), ("1", False)], keys)
    fake(77)
    seen.take()

    inject_x11.inject_key("caps_lock", True)
    inject_x11.inject_key("caps_lock", False)
    inject_x11.inject_key("a", True)
    inject_x11.inject_key("a", False)
    inject_x11.inject_key("caps_lock", True)
    inject_x11.inject_key("caps_lock", False)
    keys, _, _ = seen.take()
    letters = [k for k in names(keys) if len(k[0]) == 1]
    check("under Caps Lock a small letter is typed with Shift and reads back small", letters == [("a", True), ("a", False)], keys)

    # A second group, built by remapping two keys: Xvfb takes per-key changes but not a new XKB
    # keymap. ISO_Next_Group locks the next group, as a layout switch does.
    other = display.Display()
    try:
        other.change_keyboard_mapping(38, [(ord("a"), ord("A"), XK.string_to_keysym("Cyrillic_ef"), XK.string_to_keysym("Cyrillic_EF"))])
        other.change_keyboard_mapping(250, [(XK.string_to_keysym("ISO_Next_Group"),) * 2])
        other.sync()
    finally:
        other.close()
    time.sleep(0.3)
    fake(250)
    inject_x11.inject_key("ф", True)
    inject_x11.inject_key("ф", False)
    inject_x11.inject_key("Ф", True)
    inject_x11.inject_key("Ф", False)
    keys, _, _ = seen.take()
    check("a second layout group is typed and read back", [k for k in names(keys) if len(k[0]) == 1] == [("ф", True), ("ф", False), ("Ф", True), ("Ф", False)], keys)
    fake(250)
    seen.take()

    seen.key_answer = False
    listener.presses()
    inject_x11.inject_key("w", True)
    inject_x11.inject_key("w", False)
    check("a key handed back from inside the grab reaches another app", listener.presses() == 1)
    seen.key_answer = None
    check("and the grab is held again after it", hooks.grabbed and not other_client_grab())
    away[0] = False
    wait(lambda: not hooks.grabbed)
    seen.take()

    listener.presses()
    inject_x11.inject_key("a", True)
    time.sleep(1.0)
    inject_x11.inject_key("a", True)  # the peer's repeat
    inject_x11.inject_key("a", False)
    count = listener.presses()
    check("a held plain key is repeated by the server, the peer's repeat not pressed again", count > 3, count)
    inject_x11.inject_key("!", True)
    time.sleep(1.0)
    held_once = listener.presses()
    inject_x11.inject_key("!", True)
    inject_x11.inject_key("!", False)
    again = listener.presses()
    check("a key typed with Shift around it is not repeated by the server", held_once == 2, held_once)  # Shift and 1
    check("and each of the peer's repeats types it again", again == 2, again)
    other = display.Display()
    try:
        repeats = other.get_keyboard_control().auto_repeats
    finally:
        other.close()
    check("its auto-repeat is back on after the release", bool(repeats[10 // 8] >> (10 % 8) & 1))
    seen.take()


def proof_killed_process_lets_go():
    child = subprocess.Popen([sys.executable, __file__, "grab-and-hang"], stdout=subprocess.PIPE, text=True)
    line = child.stdout.readline().strip()
    check("a child process holds the grab", line == "grabbed" and not other_client_grab(), line)
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    check("a killed process's grab goes with its socket", wait(other_client_grab))


def grab_and_hang():
    import capture_x11

    hooks = capture_x11.Hooks(lambda *a: None, lambda *a: None, lambda *a: None)
    hooks.grab_while(lambda: True)
    hooks.start()
    wait(lambda: hooks.grabbed)
    print("grabbed", flush=True)
    time.sleep(60)


def quit_soon(app):
    """app.quit from any thread: a QTimer started off the GUI thread never fires."""
    from PySide6.QtCore import QMetaObject, Qt

    QMetaObject.invokeMethod(app, "quit", Qt.ConnectionType.QueuedConnection)


def png_bytes():
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(3, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("teal"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def proof_clipboard():
    """In a child with a real xcb Qt application, as Beamer runs."""
    done = subprocess.run([sys.executable, __file__, "clipboard-owner"], capture_output=True, text=True, timeout=60,
                          env=dict(os.environ, QT_QPA_PLATFORM="xcb"))
    sys.stdout.write(done.stdout)
    if done.returncode != 0:
        check("the clipboard proof ran", False, done.stderr[-2000:])
    failures.extend(line[5:] for line in done.stdout.splitlines() if line.startswith("FAIL "))


def clipboard_owner():
    from PySide6.QtGui import QGuiApplication

    import clipboard_x11

    app = QGuiApplication([])
    png = png_bytes()

    def other(*args, **kwargs):
        return subprocess.Popen([sys.executable, __file__, "clipboard-other", *args], stdout=subprocess.PIPE,
                                stdin=subprocess.PIPE, text=True, **kwargs)

    def run():
        # From a worker thread, as the receiver and the sender call it.
        try:
            check("Beamer writes text and an image", clipboard_x11.set_contents("from beamer", png))
            time.sleep(0.3)
            check("its own write is not a change", clipboard_x11.changed_contents() == (None, None))
            reader = other("read")
            text, digest = reader.stdout.readline().rstrip("\n").split("\t")
            reader.wait()
            check("another app pastes Beamer's text", text == "from beamer", text)
            check("another app pastes Beamer's image byte for byte", digest == hashlib.sha256(png).hexdigest(), digest)
            writer = other("write")
            ready = writer.stdout.readline().strip()
            time.sleep(0.5)
            got = clipboard_x11.changed_contents()
            writer.stdin.close()
            writer.wait()
            check("another app's copy is a change, read from its owner", ready == "set" and got == ("from another app", None), got)
            check("and read once", clipboard_x11.changed_contents() == (None, None))
        except Exception as exc:
            check("the clipboard proof finished", False, repr(exc))
        finally:
            quit_soon(app)

    threading.Thread(target=run, daemon=True).start()
    app.exec()
    sys.exit(1 if failures else 0)


def clipboard_other(action):
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication([])
    clipboard = app.clipboard()
    if action == "read":
        def read():
            mime = clipboard.mimeData()
            data = bytes(mime.data("image/png")) if mime is not None and mime.hasFormat("image/png") else b""
            print(f"{clipboard.text()}\t{hashlib.sha256(data).hexdigest()}", flush=True)
            app.quit()

        QTimer.singleShot(0, read)
    else:
        def write():
            clipboard.setText("from another app")
            print("set", flush=True)

        # Owned until the owner has read it: an X selection dies with its owner.
        threading.Thread(target=lambda: (sys.stdin.read(), quit_soon(app)), daemon=True).start()
        QTimer.singleShot(0, write)
    app.exec()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "grab-and-hang":
        grab_and_hang()
    elif len(sys.argv) > 1 and sys.argv[1] == "clipboard-owner":
        clipboard_owner()
    elif len(sys.argv) > 1 and sys.argv[1] == "clipboard-other":
        clipboard_other(sys.argv[2])
    else:
        proof_injection_and_capture()
        proof_killed_process_lets_go()
        proof_clipboard()
        print(f"{len(failures)} failed" if failures else "all passed", flush=True)
        sys.exit(min(len(failures), 100))
