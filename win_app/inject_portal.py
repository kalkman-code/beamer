"""Keyboard and mouse injection on Wayland, through the RemoteDesktop portal and libei: the twin of
inject_x11.py, with the same functions, so the receiver drives it unchanged. The portal clipboard
rides on the same session (clipboard_portal), since GNOME offers its clipboard to a background app
only there.

One thread owns the session: its D-Bus connection, the libei sender and the clipboard's transfers.
The functions here hand it work and return; only a clipboard read waits for an answer. The session
starts when start() is called (the app does so when it can be driven or shares a clipboard) or at
the first event, and the desktop asks the user once: the answer is remembered with a restore
token (persist_mode 2), kept in the state directory, so later starts ask nothing.

Keys are pressed by evdev code on the keyboard the desktop gives, whose layout it hands over with
the device and whose locks and group it reports as they change. A character is typed as on X11
(inject_x11.plan_key: the layout's own key, Shift worked around it, the US place under a chord),
with two differences. The desktop's keymap cannot be changed, so a character the layout has no key
for is not typed (logged once). And a held key is repeated by the app it reaches, not the server,
so a key typed with Shift changed around it is let go at once and each of the peer's repeats types
it again.

The pointer is moved with libei's absolute pointer where the desktop offers one (GNOME does), at
a position Beamer keeps in desktop_portal, since no Wayland app can ask where the pointer is."""

import logging
import os
import queue
import select
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

import desktop_portal
import portal
from capture_x11 import KEYCODE_OFFSET, LOCK_MASK, NUM_LOCK_MASK
from core.return_edge import Rect
from inject_x11 import (
    MAP,
    NO_REPEAT,
    PRESS,
    RELEASE,
    REPEAT,
    KeyState,
    Layout,
    plan_key,
    plan_text,
)

LOGGER = logging.getLogger(__name__)

INTERFACE = "org.freedesktop.portal.RemoteDesktop"
CLIPBOARD = "org.freedesktop.portal.Clipboard"
DEVICE_KEYBOARD, DEVICE_POINTER = 1, 2
TICK_SECONDS = 0.1
CALL_SECONDS = 2.0
# How long the portal has to answer a request no user sees.
REQUEST_SECONDS = 10.0
# After a session ends (not a refusal), the next one is opened no sooner than this.
RETRY_SECONDS = 5.0
# How long the desktop has, once connected, to offer the devices input waits for.
DEVICE_SECONDS = 2.0
# Input waiting for the session beyond this is dropped, not queued without end.
MAX_PENDING = 512
# How long an app pasting Beamer's clipboard has to take it.
WRITE_SECONDS = 5.0
# libei's capabilities (libei.py), here so this module imports without libei.
CAP_POINTER, CAP_POINTER_ABSOLUTE, CAP_KEYBOARD, CAP_SCROLL, CAP_BUTTON = 1, 2, 4, 16, 32

# evdev button codes.
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112, "back": 0x113, "forward": 0x114}
# A wheel click as libei counts it, and the pixels the PC reckons a click scrolls (inject_x11).
DISCRETE_PER_CLICK = 120
PIXELS_PER_CLICK = 40.0

NO_PORTAL = ("This desktop does not let apps control the keyboard and mouse (it has no RemoteDesktop "
             "portal with libei), so another machine cannot drive this one and the clipboard is not shared.")
REFUSED = ("Beamer was not allowed to control this machine, so another machine cannot drive it and the "
           "clipboard is not shared. Restart Beamer to be asked again.")
ENDED = "The desktop stopped letting Beamer control this machine: {detail}"
CLOSED = ("Remote control was stopped from the desktop, so another machine cannot drive this one and the "
          "clipboard is not shared until Beamer restarts.")
MISSING = "Beamer cannot control this machine: {detail}"

# Called with one of the sentences above when the session cannot start or ends; set by the app.
on_problem: Optional[Callable[[str], None]] = None


class WaylandLayout(Layout):
    """inject_x11's Layout over the desktop's keymap, which cannot be changed: no spare keys."""

    def spare(self) -> List[int]:
        return []


def wayland_steps(steps: list) -> List[Tuple[int, bool]]:
    """plan_key's or plan_text's steps as (evdev code, down). A key after NO_REPEAT is let go as soon
    as it is pressed; REPEAT, the X server's, means nothing here, and MAP cannot be planned with no
    spare keys."""
    out: List[Tuple[int, bool]] = []
    quick: Set[int] = set()
    for step in steps:
        kind, keycode = step[0], step[1]
        if kind == NO_REPEAT:
            quick.add(keycode)
        elif kind == PRESS:
            out.append((keycode - KEYCODE_OFFSET, True))
            if keycode in quick:
                out.append((keycode - KEYCODE_OFFSET, False))
        elif kind == RELEASE:
            out.append((keycode - KEYCODE_OFFSET, False))
        elif kind not in (REPEAT, MAP):
            LOGGER.warning("Unknown key step %r", step)
    return out


def scroll_steps(dy: float, dx: float, mode: str, carry: List[float]) -> Tuple[str, float, float]:
    """One scroll message as libei's: ("discrete", x, y) in 120ths of a click for the wheel's lines,
    the fraction carried in `carry` ([y, x]); ("pixels", x, y) for a pixel scroll. The wire's y is
    positive away from the hand, libei's positive down."""
    if mode == "pixel":
        return "pixels", float(dx), -float(dy)
    carry[0] += float(dy) * DISCRETE_PER_CLICK
    carry[1] += float(dx) * DISCRETE_PER_CLICK
    whole_y, whole_x = int(carry[0]), int(carry[1])
    carry[0] -= whole_y
    carry[1] -= whole_x
    return "discrete", whole_x, -whole_y


def _keymap_from_text(text: str):
    import xkb_x11

    return xkb_x11.Keymap.from_text(text)


def _ei_sender(fd: int):
    import libei

    return libei.Context(fd, sender=True)


def _libei_present() -> None:
    import libei

    libei._load()


# Checked before the session is asked for, so the desktop never asks for something Beamer cannot use.
_ei_sender.preflight = _libei_present


class Remote:
    """The RemoteDesktop session's thread. `submit(fn)` runs fn(self) there once the desktop's
    devices are up; `ask(fn)` runs it there at once and returns its answer.

    A refusal, or a desktop with no portal for it, stands until Beamer restarts, and no work is
    queued meanwhile. A session that ends any other way opens again at the next input, after
    RETRY_SECONDS, without asking: its restore token says the user already said yes."""

    def __init__(self, connect: Callable[[], object] = None, ei: Callable[[int], object] = None, tokens=None,
                 keymap: Callable[[str], object] = None) -> None:
        self._connect = connect or portal.JeepneyBus
        self._ei_factory = ei or _ei_sender
        self._keymap = keymap or _keymap_from_text
        self._tokens = tokens or portal.TokenStore("remote-desktop")
        self._commands: "queue.Queue" = queue.Queue()
        self._wake_read, self._wake_write = os.pipe()
        os.set_blocking(self._wake_read, False)
        os.set_blocking(self._wake_write, False)
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self.ready = threading.Event()
        self.problem: Optional[str] = None
        self._terminal = False   # refused or impossible: never asked again this run
        self._closed = False     # stopped for good (Beamer quitting)
        self._retry_at = 0.0
        self._said_full = False
        self._generation = 0
        self.stamp = 0  # never reset, so a new session cannot repeat a stamp a caller holds
        self._reset()

    def _reset(self) -> None:
        self.portal = None
        self.ei = None
        self.handle = None
        self.clipboard_enabled = False
        self._devices: List[object] = []   # resumed, in the order they came
        self._added: List[object] = []
        self._sequence = 0
        self._eis_at: Optional[float] = None
        self._held: List[Callable] = []    # input waiting for the devices to resume
        self.layout: Optional[WaylandLayout] = None
        self.group = 0
        self.locks = 0
        self.state = KeyState()
        self.pressed: Set[int] = set()
        self.buttons: Dict[int, object] = {}
        self.scroll_carry = [0.0, 0.0]
        # The clipboard: what another app offers, what Beamer offers, and a count of other apps'
        # copies. `stamp` moves at every change, Beamer's own writes included (clipboard_portal).
        self.offered: List[str] = []
        self.ours = False
        self.changes = 0
        self.offer: Dict[str, bytes] = {}

    # -- from any thread ---------------------------------------------------------

    def _admits(self) -> bool:
        return not (self._closed or self._terminal)

    def start(self) -> None:
        with self._lock:
            if not self._admits():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            if time.monotonic() < self._retry_at:
                return
            self._stopping.clear()
            self.ready.clear()
            self._thread = threading.Thread(target=self._run, name="Beamer-remote", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """For good: nothing after it opens the session again."""
        with self._lock:
            self._closed = True
            self._stopping.set()
        self._wake()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            session = self.portal
            if thread.is_alive() and session is not None:
                abort = getattr(session.bus, "abort", None)
                if abort is not None:
                    abort()

    def submit(self, work: Callable[["Remote"], None], release: bool = False) -> None:
        """`release` work (a key or button let go) is never dropped for a full queue: dropping it
        would leave the press held on this machine."""
        if not self._admits():
            return
        self.start()
        if self._commands.qsize() >= MAX_PENDING and not release:
            if not self._said_full:
                self._said_full = True
                LOGGER.warning("Remote control is not keeping up; input from the peer is dropped until it does")
            return
        self._said_full = False
        thread = self._thread
        if thread is None or not thread.is_alive():
            return  # between sessions: input that late would play stale
        self._commands.put((work, None, None))
        self._wake()

    def ask(self, work: Callable[["Remote"], object], default=None, timeout: float = CALL_SECONDS,
            discard: Optional[Callable[[object], None]] = None):
        """Run `work` on the session's thread and return what it answers, or `default` when the
        session is not up or does not answer in time; an answer that comes after that is handed
        to `discard` (an fd is closed, say)."""
        if not self._admits():
            return default
        self.start()
        if not self.ready.wait(timeout):
            return default
        answer = _Answer(discard)
        self._commands.put((work, answer, None))
        self._wake()
        return answer.wait(timeout, default)

    def _wake(self) -> None:
        try:
            os.write(self._wake_write, b"x")
        except (BlockingIOError, OSError):
            pass

    # -- the thread --------------------------------------------------------------

    def _run(self) -> None:
        ended = False
        try:
            preflight = getattr(self._ei_factory, "preflight", None)
            try:
                if preflight is not None:
                    preflight()
                bus = self._connect()
            except Exception as exc:
                # Missing libei or jeepney, or no session bus: no retry would find them.
                raise _Problem(MISSING.format(detail=exc), terminal=True) from exc
            self.portal = portal.Portal(bus, self._stopping.is_set)
            self._open()
            while not self._stopping.is_set():
                self._wait()
                for path, interface, member, body in self.portal.signals():
                    self._signal(path, interface, member, body)
                for event in self.ei.events():
                    self._ei_event(event)
                self._run_commands()
        except _Problem as problem:
            ended = not problem.terminal
            self._terminal = problem.terminal
            self._report(problem.sentence)
        except Exception as exc:
            if not self._stopping.is_set():
                LOGGER.exception("Remote control stopped")
                ended = True
                self._report(ENDED.format(detail=exc))
        finally:
            if ended:
                if self._tokens.read():
                    self._retry_at = time.monotonic() + RETRY_SECONDS
                else:
                    # Opened again it would ask again, prompted by the peer's input: it waits for a restart.
                    self._terminal = True
            self._end()

    def _report(self, sentence: str) -> None:
        if sentence == self.problem:
            return  # said already, and no session has opened since
        self.problem = sentence
        LOGGER.warning("%s", sentence)
        if on_problem is not None:
            try:
                on_problem(sentence)
            except Exception:
                LOGGER.exception("Could not report a remote control problem")

    def _open(self) -> None:
        portal_ = self.portal
        version = portal_.version(INTERFACE)
        if version == 1:
            raise _Problem(NO_PORTAL, terminal=True)
        created = portal_.request(INTERFACE, "CreateSession", "a{sv}", lambda token: (
            {"handle_token": ("s", token), "session_handle_token": ("s", portal_.token())},), timeout=REQUEST_SECONDS)
        self.handle = created["session_handle"]
        options = {"types": ("u", DEVICE_KEYBOARD | DEVICE_POINTER)}
        if version >= 2:
            options["persist_mode"] = ("u", 2)
            token = self._tokens.read()
            if token:
                options["restore_token"] = ("s", token)
        portal_.request(INTERFACE, "SelectDevices", "oa{sv}", lambda handle_token: (
            self.handle, dict(options, handle_token=("s", handle_token))), timeout=REQUEST_SECONDS)
        try:
            portal_.call(CLIPBOARD, "RequestClipboard", "oa{sv}", (self.handle, {}))
        except portal.PortalError:
            LOGGER.info("The desktop offers no clipboard with remote control")
        try:
            started = portal_.request(INTERFACE, "Start", "osa{sv}", lambda handle_token: (
                self.handle, "", {"handle_token": ("s", handle_token)}))
        except portal.Refused:
            self._tokens.write("")
            raise _Problem(REFUSED, terminal=True) from None
        self._tokens.write(started.get("restore_token", ""))
        self.clipboard_enabled = bool(started.get("clipboard_enabled"))
        fd = portal_.call(INTERFACE, "ConnectToEIS", "oa{sv}", (self.handle, {}))[0]
        try:
            self.ei = self._ei_factory(fd)
        except Exception:
            os.close(fd)
            raise
        self._generation += 1
        self._eis_at = time.monotonic()
        self.problem = None
        self.ready.set()
        desktop_portal.set_mover(lambda x, y: self.submit(lambda remote: remote.place(x, y)))

    def _wait(self) -> None:
        if self.portal.pending() or (not self._commands.empty() and self._input_ready()):
            return
        try:
            ready, _, _ = select.select([self.portal.fileno(), self.ei.fileno(), self._wake_read], [], [], TICK_SECONDS)
        except (OSError, ValueError) as exc:
            raise _Problem(ENDED.format(detail=exc)) from exc
        if self._wake_read in ready:
            try:
                while os.read(self._wake_read, 256):
                    pass
            except BlockingIOError:
                pass

    def _input_ready(self) -> bool:
        """Whether input can be played: a keyboard and a pointer are up, or the desktop has had
        DEVICE_SECONDS to offer them and what is up is all there will be."""
        if self._with(CAP_KEYBOARD) is not None and (self._with(CAP_POINTER_ABSOLUTE) or self._with(CAP_POINTER)):
            return True
        return self._eis_at is not None and time.monotonic() - self._eis_at >= DEVICE_SECONDS

    def _run_commands(self) -> None:
        while True:
            try:
                work, answer, _ = self._commands.get_nowait()
            except queue.Empty:
                break
            if answer is None:
                self._held.append(work)
                continue
            try:
                answer.give(True, work(self))
            except Exception:
                LOGGER.exception("Could not answer a clipboard request")
                answer.give(False, None)
        if not self._held or not self._input_ready():
            return
        held, self._held = self._held, []
        for work in held:
            try:
                work(self)
            except Exception:
                LOGGER.exception("Could not play an input from the peer")

    def _end(self) -> None:
        desktop_portal.set_mover(None)
        self.ready.clear()
        try:
            if self.ei is not None:
                self.release_held()
        except Exception:
            LOGGER.exception("Could not let go of what was held for the peer")
        try:
            if self.ei is not None:
                self.ei.close()
        finally:
            if self.portal is not None:
                self.portal.close()
            self._reset()
        # Work still queued answers nothing; the next start begins clean.
        while True:
            try:
                _work, answer, _ = self._commands.get_nowait()
            except queue.Empty:
                break
            if answer is not None:
                answer.give(False, None)

    # -- signals and devices -----------------------------------------------------

    def _signal(self, path, interface, member, body) -> None:
        if interface == portal.SESSION and member == "Closed" and path == self.handle:
            # The user stopped it from the desktop's indicator: it is not opened again behind them.
            raise _Problem(CLOSED, terminal=True)
        if interface != CLIPBOARD or not body or body[0] != self.handle:
            return
        if member == "SelectionOwnerChanged":
            options = body[1] if len(body) > 1 else {}
            self.ours = bool(options.get("session_is_owner"))
            if not self.ours:
                self.offered = list(options.get("mime_types", []))
                self.changes += 1
                self.stamp += 1
        elif member == "SelectionTransfer":
            try:
                self._transfer(body[1], body[2])
            except portal.PortalError:
                # One paste lost; the session and the clipboard carry on.
                LOGGER.exception("Could not hand the clipboard to the app pasting it")
                self._write_done(body[2], False)

    def _write_done(self, serial: int, ok: bool) -> None:
        try:
            self.portal.call(CLIPBOARD, "SelectionWriteDone", "oub", (self.handle, serial, ok))
        except portal.PortalError:
            LOGGER.exception("Could not tell the desktop a paste has finished")

    def _ei_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "device_added":
            self._add(event[1])
        elif kind == "device_removed":
            device = event[1]
            for listed in (self._devices, self._added):
                if device in listed:
                    listed.remove(device)
            # Buttons it held went with it: the desktop lets go of a removed device's input.
            self.buttons = {code: held for code, held in self.buttons.items() if held is not device}
        elif kind == "device_resumed":
            device = event[1]
            if device not in self._devices:
                self._devices.append(device)
            self._sequence += 1
            self.ei.start(device, self._sequence)
        elif kind == "device_paused":
            if event[1] in self._devices:
                self._devices.remove(event[1])
        elif kind == "modifiers" and self.layout is not None:
            _device, _depressed, _latched, locked, group = event[1:6]
            self.group = group
            self.locks = self.layout.keymap.core_mods(locked) & (LOCK_MASK | NUM_LOCK_MASK)
        elif kind == "disconnect":
            raise _Problem(ENDED.format(detail="its input connection closed"))

    def _add(self, device) -> None:
        self._added.append(device)
        if device.has(CAP_POINTER_ABSOLUTE):
            regions = [Rect(x, y, width, height) for x, y, width, height, _scale in device.regions()]
            if regions:
                desktop_portal.set_regions(regions)
        if device.has(CAP_KEYBOARD) and self.layout is None:
            try:
                text = device.keymap()
                if text:
                    self.layout = WaylandLayout(self._keymap(text))
            except Exception:
                LOGGER.exception("Could not read this desktop's keyboard layout; characters cannot be typed")

    def _with(self, cap: int):
        """The first resumed device with `cap`, or None."""
        return next((device for device in self._devices if device.has(cap)), None)

    # -- playing input, on the thread ---------------------------------------------

    def keys(self, presses: List[Tuple[int, bool]]) -> None:
        device = self._with(CAP_KEYBOARD)
        if device is None:
            return
        for code, down in presses:
            if down:
                self.pressed.add(code)
            elif code in self.pressed:
                self.pressed.discard(code)
            else:
                continue
            self.ei.key(device, code, down)
            self.ei.frame(device)

    def move_by(self, dx: float, dy: float) -> None:
        x, y = desktop_portal.cursor_position()
        if self._with(CAP_POINTER_ABSOLUTE) is not None:
            self.place(x + dx, y + dy)
            return
        device = self._with(CAP_POINTER)
        if device is not None:
            self.ei.motion(device, dx, dy)
            self.ei.frame(device)
            desktop_portal.note_position(x + dx, y + dy)

    def place(self, x: float, y: float) -> None:
        desktop_portal.note_position(x, y)
        point = desktop_portal.position()
        device = self._with(CAP_POINTER_ABSOLUTE)
        if device is not None:
            self.ei.absolute(device, float(point[0]), float(point[1]))
            self.ei.frame(device)

    def button(self, code: int, down: bool) -> None:
        if down:
            device = self._with(CAP_BUTTON)
            if device is None:
                return
            self.buttons[code] = device
        else:
            device = self.buttons.pop(code, None)
            if device is None:
                return
        self.ei.button(device, code, down)
        self.ei.frame(device)

    def scroll(self, dy: float, dx: float, mode: str) -> None:
        device = self._with(CAP_SCROLL)
        if device is None:
            return
        kind, x, y = scroll_steps(dy, dx, mode, self.scroll_carry)
        if kind == "pixels":
            self.ei.scroll(device, x, y)
        elif x or y:
            self.ei.scroll_discrete(device, int(x), int(y))
        else:
            return
        self.ei.frame(device)

    def release_held(self) -> None:
        self.keys([(code, False) for code in sorted(self.pressed)])
        for code in sorted(self.buttons):
            self.button(code, False)
        self.state = KeyState()
        self.scroll_carry = [0.0, 0.0]

    # -- the clipboard, on the thread ----------------------------------------------

    def set_selection(self, offer: Dict[str, bytes]) -> bool:
        if not self.clipboard_enabled:
            return False
        self.offer = dict(offer)
        self.portal.call(CLIPBOARD, "SetSelection", "oa{sv}", (self.handle, {"mime_types": ("as", list(offer))}))
        self.ours = True
        self.stamp += 1
        return True

    def open_selection(self, mime: str) -> Optional[int]:
        """An fd the owning app writes `mime`'s data into, for the caller to read and close."""
        if not self.clipboard_enabled:
            return None
        return self.portal.call(CLIPBOARD, "SelectionRead", "os", (self.handle, mime))[0]

    def _transfer(self, mime: str, serial: int) -> None:
        """Another app pastes what Beamer put on the clipboard: its data goes down an fd the portal
        gives, written on a helper thread so a slow reader never holds this one, within
        WRITE_SECONDS, and the portal is told it is done from here, by this session only."""
        data = self.offer.get(mime)
        if data is None:
            self._write_done(serial, False)
            return
        fd = self.portal.call(CLIPBOARD, "SelectionWrite", "ou", (self.handle, serial))[0]
        generation = self._generation
        stopping = self._stopping

        def write() -> None:
            ok = _write_all(fd, data, WRITE_SECONDS, stopping)

            def done(remote: "Remote") -> None:
                if remote._generation == generation and remote.portal is not None:
                    remote._write_done(serial, ok)

            self._commands.put((done, _Answer(None), None))
            self._wake()

        threading.Thread(target=write, name="Beamer-clipboard-write", daemon=True).start()


def _write_all(fd: int, data: bytes, seconds: float, stopping: threading.Event) -> bool:
    """Write `data` to `fd` and close it, giving up after `seconds` or when the session stops."""
    deadline = time.monotonic() + seconds
    try:
        os.set_blocking(fd, False)
        view = memoryview(data)
        while view:
            left = deadline - time.monotonic()
            if left <= 0 or stopping.is_set():
                LOGGER.warning("The app pasting the clipboard did not take it in time")
                return False
            _, ready, _ = select.select([], [fd], [], min(left, 0.2))
            if ready:
                view = view[os.write(fd, view):]
        return True
    except OSError:
        LOGGER.exception("Could not hand the clipboard to the app pasting it")
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class _Answer:
    """One answer from the session's thread to a caller who waits a bounded time; an answer that
    comes after the caller gave up is handed to `discard`."""

    def __init__(self, discard: Optional[Callable[[object], None]]) -> None:
        self._discard = discard
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._gave_up = False
        self._value = (False, None)

    def give(self, ok: bool, value) -> None:
        with self._lock:
            if not self._gave_up:
                self._value = (ok, value)
                self._event.set()
                return
        if ok and self._discard is not None and value is not None:
            try:
                self._discard(value)
            except Exception:
                LOGGER.exception("Could not discard a late answer")

    def wait(self, timeout: float, default):
        self._event.wait(timeout)
        with self._lock:
            if not self._event.is_set():
                self._gave_up = True
                return default
        ok, value = self._value
        return value if ok else default


class _Problem(Exception):
    def __init__(self, sentence: str, terminal: bool = False) -> None:
        super().__init__(sentence)
        self.sentence = sentence
        self.terminal = terminal


_remote: Optional[Remote] = None
_remote_lock = threading.Lock()


def remote() -> Remote:
    global _remote
    with _remote_lock:
        if _remote is None:
            _remote = Remote()
        return _remote


def start() -> None:
    """Open the session now, so the desktop asks while the user is at this machine."""
    remote().start()


def stop() -> None:
    if _remote is not None:
        _remote.stop()


def _warn_once(message: str, item: str, _said: Set[str] = set()) -> None:
    if item not in _said:
        _said.add(item)
        LOGGER.warning(message, item)


def inject_key(name: str, down: bool, us: Optional[str] = None) -> None:
    def go(session: Remote) -> None:
        if session.layout is None and len(name) == 1:
            return  # a named key needs no layout; a character does
        session.keys(wayland_steps(plan_key(name, down, us, session.state, session.layout, session.group, session.locks)))

    remote().submit(go, release=not down)


def inject_text(text: str) -> None:
    def go(session: Remote) -> None:
        if session.layout is None:
            return
        session.keys(wayland_steps(plan_text(text, session.state, session.layout, session.group, session.locks)))

    remote().submit(go)


def inject_mouse_move(dx: int, dy: int) -> None:
    remote().submit(lambda session: session.move_by(dx, dy))


def move_to(x: int, y: int) -> None:
    remote().submit(lambda session: session.place(x, y))


def inject_mouse_button(button: str, down: bool) -> None:
    code = BUTTONS.get(button)
    if code is None:
        _warn_once("Unknown mouse button ignored: %r", button)
        return
    remote().submit(lambda session: session.button(code, down), release=not down)


def inject_scroll(dy, dx=0.0, mode: str = "line") -> None:
    remote().submit(lambda session: session.scroll(dy, dx, mode))


def inject_gesture(name: str, swipe=None) -> None:
    """As on X11: a Linux machine does not offer `gestures`."""
    _warn_once("Gestures are not yet on Linux; %r dropped", name)


def release_all() -> None:
    """Let go of every key and button held for the peer."""
    if _remote is not None:
        _remote.submit(lambda session: session.release_held(), release=True)
