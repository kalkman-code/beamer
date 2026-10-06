"""Keyboard and mouse capture on Wayland, through the InputCapture portal and libei: the twin of
capture_x11.py, with the same Hooks, Trigger and names, so the bridge and the sender drive it.

Wayland lets no app see input meant for another, so capture is the desktop's. Beamer puts pointer
barriers on the outside edges of this machine's zones (the edge, its thirds, or a corner's two
walls) and the compositor, when the pointer pushes through one, captures every keyboard and
pointer and sends their events to Beamer over libei until Beamer lets go (Release). Nothing else
can start a capture, so input leaves this machine only across an edge: the shortcut and the tray
bring it home but cannot send it away, and Beamer sees no key at all while input is here.

A push into a barrier is not yet a crossing. The barrier holds the pointer, as Windows holds it at
the edge of the desktop, and the movement that follows feeds the sender's own zone models, so the
resistance, the corners and the thirds behave as on the other machines. The pointer can slide
along the barrier while held; the desktop shows it still, so where it is lives in desktop_portal
and the pointer is let go there. Any movement back into the screen, a key, a button, or a pause
in the push lets go of it; the key or button that did is lost, since nothing can play it back
here. Once the zone gives, input is away until the sender brings it home, and the pointer is let
go where the sender lands it (it says so through desktop_portal before or just after it reports
input home, so the let-go waits a moment for it).

While input is away, a key the sender hands back (the stays-here list) is lost for the same
reason, and the full-screen hold and the drag guard have nothing to read (desktop_portal).

Every way home is the desktop's: Release, the session closing when its D-Bus connection does
(a stop, an error, the process dying), and a watchdog that ends that connection when the capture
thread stops answering while the desktop holds input for it.

InputCapture v2 (GNOME 51, Plasma 6.7) remembers the user's answer with a restore token, kept in
the state directory; v1 asks at every start."""

import logging
import math
import os
import select
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from capture_win import (  # noqa: F401  (re-exported for the bridge, which reads them from here)
    BUTTON_TITLES,
    MODIFIER_NAMES,
    REDIRECT,
    RETURN,
    TOGGLE,
    WHEEL_DELTA,
    WM_MOUSEHWHEEL,
    WM_MOUSEWHEEL,
    Trigger,
    button_of,
    mouse_event,
    wheel_notches,
)
from capture_x11 import (  # noqa: F401  (the same evdev names and titles as X11)
    _BUTTONS,
    CHORD_MASK,
    KEYCODE_OFFSET,
    NOT_TRIGGER_VKS,
    UNRECORDABLE_TRIGGER_VKS,
    VK_TITLES,
    VK_TO_NAME,
    _wheel_data,
    character,
    hook_vk,
    input_title,
)
from core import keytable
from core import return_edge as crossing
import desktop_portal
import portal

LOGGER = logging.getLogger(__name__)

INTERFACE = "org.freedesktop.portal.InputCapture"
CAP_KEYBOARD, CAP_POINTER = 1, 2
TICK_SECONDS = 0.05
STALL_SECONDS = 2.0
# A push that stops for this long is a pointer resting at the edge, which is let go.
IDLE_SECONDS = 0.3
# How long a let-go waits for the sender to say where the pointer lands.
LANDING_SECONDS = 0.15
# How long the portal has to answer a request no user sees (zones, barriers).
REQUEST_SECONDS = 10.0
# After the desktop disables the session, the barriers are set again no sooner than this.
DISABLED_SECONDS = 1.0
# An Activated signal with no activation_id (the portal spec allows it): libei's sequences are not
# checked, and Release names none.
NO_ID = -1
# A push that met two barriers at once is put on the nearest within this many pixels of the pointer.
WALL_PX = 64
# While input is away with nothing captured, the sender is asked to bring it home this often.
HOME_AGAIN_SECONDS = 0.5
# ei events that arrive before the portal's Activated signal says which barrier, held until it does.
EARLY_MAX = 64
EARLY_SECONDS = 1.0
# libei's scroll is in the desktop's pixels; mutter and weston send 10 for one wheel click.
PIXELS_PER_NOTCH = 10.0
# evdev's button codes, as X numbers them for capture_x11's table.
EVDEV_BUTTONS = {0x110: 1, 0x111: 3, 0x112: 2, 0x113: 8, 0x114: 9}

# The core modifier bits a held key sets, for when the desktop does not say (libei sends a
# receiver its modifiers only if the compositor chooses to).
HELD_MODS = {42: 1, 54: 1, 29: 4, 97: 4, 56: 8, 125: 64, 126: 64}

# Wall outward directions.
_NORMAL = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}

# The app's own sentences for what Wayland takes away; the bridge shows them as they are.
NO_PORTAL = ("This desktop does not let apps capture the keyboard and mouse (it has no InputCapture "
             "portal), so this machine cannot drive another one. It can still be driven if the "
             "desktop allows remote control.")
REFUSED = ("Beamer was not allowed to capture the keyboard and mouse, so this machine cannot drive "
           "another one. Turn sending off and on again to be asked again.")
ENDED = "The desktop stopped letting Beamer capture the keyboard and mouse: {detail}"


class CaptureUnavailable(RuntimeError):
    """Capture cannot happen here; `sentence` says so in the app's words."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


# -- barriers, pure -------------------------------------------------------


def _outer(monitors: List[crossing.Rect], monitor: crossing.Rect, edge: str) -> List[Tuple[int, int]]:
    """The stretches of `monitor`'s `edge`, as [start, end) along it, that no other display lies
    beyond: a barrier must sit on the outside of the union of the zones."""
    if edge in ("left", "right"):
        line = monitor.x if edge == "left" else monitor.x + monitor.width
        start, end = monitor.y, monitor.y + monitor.height
        beyond = [(m.y, m.y + m.height) for m in monitors
                  if (m.x + m.width == line if edge == "left" else m.x == line)]
    else:
        line = monitor.y if edge == "top" else monitor.y + monitor.height
        start, end = monitor.x, monitor.x + monitor.width
        beyond = [(m.x, m.x + m.width) for m in monitors
                  if (m.y + m.height == line if edge == "top" else m.y == line)]
    stretches = [(start, end)]
    for low, high in beyond:
        cut = []
        for a, b in stretches:
            if high <= a or low >= b:
                cut.append((a, b))
                continue
            if a < low:
                cut.append((a, low))
            if high < b:
                cut.append((high, b))
        stretches = cut
    return stretches


def _line(monitor: crossing.Rect, edge: str) -> int:
    return {"left": monitor.x, "right": monitor.x + monitor.width,
            "top": monitor.y, "bottom": monitor.y + monitor.height}[edge]


def _segment(monitor: crossing.Rect, edge: str, a: int, b: int) -> Tuple[int, int, int, int]:
    line = _line(monitor, edge)
    if edge in ("left", "right"):
        return (line, a, line, b - 1)
    return (a, line, b - 1, line)


def _clip(stretches, low: int, high: int) -> List[Tuple[int, int]]:
    return [(max(a, low), min(b, high)) for a, b in stretches if min(b, high) > max(a, low)]


def _thirds(monitor: crossing.Rect, edge: str) -> List[Tuple[int, int]]:
    """Each third of `monitor` along `edge`, as [start, end), split where return_edge.part_of
    splits a fraction of the display."""
    start, length = (monitor.y, monitor.height) if edge in ("left", "right") else (monitor.x, monitor.width)
    cuts = [start + math.ceil(i * length / 3) for i in range(3)] + [start + length]
    return [(cuts[i], cuts[i + 1]) for i in range(3)]


def barriers(models: list, monitors: List[crossing.Rect]) -> List[Tuple[str, Tuple[int, int, int, int]]]:
    """(wall, position) for every barrier the sender's zone models need on these displays, in the
    portal's coordinates, each once: a whole edge, the chosen thirds of it, or a corner's two walls
    of CORNER_PX + 1 pixels. A notch stretch (SpanEdge) is the Mac's and has none here."""
    found: List[Tuple[str, Tuple[int, int, int, int]]] = []

    def add(wall, position):
        if (wall, position) not in found:
            found.append((wall, position))

    for model in models:
        if isinstance(model, crossing.SpanEdge):
            continue
        for monitor in monitors:
            if isinstance(model, crossing.CornerPush):
                for wall in (model.vertical, model.horizontal):
                    along_far = wall in ("left", "right")
                    other = model.horizontal if wall in ("top", "bottom") else model.vertical
                    if along_far:  # a side wall runs along y, from the corner's top or bottom
                        low = monitor.y if other == "top" else monitor.y + monitor.height - 1 - crossing.CORNER_PX
                    else:
                        low = monitor.x if other == "left" else monitor.x + monitor.width - 1 - crossing.CORNER_PX
                    for a, b in _clip(_outer(monitors, monitor, wall), low, low + crossing.CORNER_PX + 1):
                        add(wall, _segment(monitor, wall, a, b))
                continue
            stretches = _outer(monitors, monitor, model.edge)
            if isinstance(model, crossing.PartEdge):
                thirds = _thirds(monitor, model.edge)
                chosen = [thirds[crossing.PARTS.index(part)] for part in crossing.PARTS if part in model.parts]
                stretches = [piece for low, high in chosen for piece in _clip(stretches, low, high)]
            for a, b in stretches:
                add(model.edge, _segment(monitor, model.edge, a, b))
    return found


def model_key(models: list) -> tuple:
    """What about the models places barriers, so they are set again only when it changes."""
    return tuple((type(m).__name__, m.edge, tuple(sorted(getattr(m, "parts", ()))), getattr(m, "corner", None))
                 for m in models)


class Press:
    """The pointer held at a barrier while input is still here: where it is, sliding along the
    wall, and whether the movement is still a push."""

    def __init__(self, wall: str, point: Tuple[int, int], displays: List[crossing.Rect], clock=time.monotonic) -> None:
        self.wall = wall
        self._displays = displays
        self.point = desktop_portal.clamp_to(displays, *point)
        self._clock = clock
        self._last_push = clock()
        self._exact = [float(self.point[0]), float(self.point[1])]

    def feed(self, dx: float, dy: float) -> bool:
        """True while the movement pushes outward or slides along the wall; False once it comes
        back into the screen, with `point` where it then is."""
        nx, ny = _NORMAL[self.wall]
        outward = dx * nx + dy * ny
        if outward < 0:
            self._move(dx, dy)
            return False
        # Only the part along the wall moves the pointer; the barrier holds the rest.
        self._move(dx * (1 - abs(nx)), dy * (1 - abs(ny)))
        if outward > 0:
            self._last_push = self._clock()
        return True

    def _move(self, dx: float, dy: float) -> None:
        self._exact[0] += dx
        self._exact[1] += dy
        self.point = desktop_portal.clamp_to(self._displays, *self._exact)
        # Travel past the display is not kept: the pointer cannot be there.
        for axis in (0, 1):
            if self.point[axis] != round(self._exact[axis]):
                self._exact[axis] = float(self.point[axis])

    def idle(self) -> bool:
        return self._clock() - self._last_push > IDLE_SECONDS


# -- the session ----------------------------------------------------------


class _Session:
    """The InputCapture session and its libei receiver, on the capture thread only."""

    def __init__(self, bus_factory, ei_factory, tokens, stopping) -> None:
        self.portal = portal.Portal(bus_factory(), stopping)
        self._ei_factory = ei_factory
        self._tokens = tokens
        self.handle = None
        self.ei = None
        self.zone_set = None
        self.enabled = False

    def check(self) -> None:
        preflight = getattr(self._ei_factory, "preflight", None)
        if preflight is not None:
            preflight()
        self.version = self.portal.version(INTERFACE)

    def open(self) -> None:
        """Make and start the session; the user may be asked, for as long as they take."""
        LOGGER.info("Requesting InputCapture session (portal version %d)", self.version)
        try:
            if self.version >= 2:
                try:
                    results = portal.plain(self.portal.call(INTERFACE, "CreateSession2", "a{sv}",
                                                            ({"session_handle_token": ("s", self.portal.token())},)))
                except portal.PortalError as exc:
                    raise CaptureUnavailable(f"{NO_PORTAL} ({exc})") from exc
                self.handle = results[0]["session_handle"]
                options = {"capabilities": ("u", CAP_KEYBOARD | CAP_POINTER), "persist_mode": ("u", 2)}
                token = self._tokens.read()
                if token:
                    options["restore_token"] = ("s", token)
                started = self.portal.request(INTERFACE, "Start", "osa{sv}", lambda handle_token: (
                    self.handle, "", dict(options, handle_token=("s", handle_token))))
                if started.get("restore_token"):
                    self._tokens.write(started["restore_token"])
            else:
                try:
                    results = self.portal.request(INTERFACE, "CreateSession", "sa{sv}", lambda handle_token: ("", {
                        "handle_token": ("s", handle_token),
                        "session_handle_token": ("s", self.portal.token()),
                        "capabilities": ("u", CAP_KEYBOARD | CAP_POINTER),
                    }))
                except portal.PortalError as exc:
                    raise CaptureUnavailable(f"{NO_PORTAL} ({exc})") from exc
                self.handle = results["session_handle"]
        except portal.Refused:
            self._tokens.write("")
            raise CaptureUnavailable(REFUSED) from None
        fd = self.portal.call(INTERFACE, "ConnectToEIS", "oa{sv}", (self.handle, {}))[0]
        try:
            self.ei = self._ei_factory(fd)
        except Exception:
            os.close(fd)
            raise

    def zones(self) -> List[crossing.Rect]:
        results = self.portal.request(INTERFACE, "GetZones", "oa{sv}", lambda token: (self.handle, {"handle_token": ("s", token)}),
                                      timeout=REQUEST_SECONDS)
        self.zone_set = results.get("zone_set")
        return [crossing.Rect(x, y, width, height) for width, height, x, y in results.get("zones", [])]

    def set_barriers(self, positions: List[Tuple[int, int, int, int]]) -> List[int]:
        """Barrier i + 1 for each position; the ids the desktop refused. Setting them suspends
        the session, so it is enabled again after, when there are any."""
        self.enabled = False
        listed = [{"barrier_id": ("u", index + 1), "position": ("(iiii)", tuple(position))}
                  for index, position in enumerate(positions)]
        results = self.portal.request(INTERFACE, "SetPointerBarriers", "oa{sv}aa{sv}u", lambda token: (
            self.handle, {"handle_token": ("s", token)}, listed, self.zone_set or 0), timeout=REQUEST_SECONDS)
        if positions:
            self.enable()
        return list(results.get("failed_barriers", []))

    def enable(self) -> None:
        self.portal.call(INTERFACE, "Enable", "oa{sv}", (self.handle, {}))
        self.enabled = True

    def release(self, activation_id: int, point: Tuple[int, int]) -> None:
        options = {"cursor_position": ("(dd)", (float(point[0]), float(point[1])))}
        if activation_id != NO_ID:
            options["activation_id"] = ("u", int(activation_id))
        self.portal.call(INTERFACE, "Release", "oa{sv}", (self.handle, options))

    def close(self) -> None:
        try:
            if self.ei is not None:
                self.ei.close()
        finally:
            self.ei = None
            self.portal.close()


class Hooks:
    """The capture thread and the session it owns, with capture_x11.Hooks' callbacks:
    `on_key(name, down, vk, us)`, `on_mouse(message, x, y, mouse_data)` and `on_motion(dx, dy)`,
    `vk` the evdev code. `grab_while(predicate)` says when input is away; `barriers_from(models)`
    gives the sender's zone models, empty while the edges are held. `on_home` is called on the
    capture thread when the desktop ends a capture while input is away (so it comes home), and
    `on_failure(exc)` when capture stops on its own after it started."""

    def __init__(self, on_key, on_mouse, on_motion, connect: Optional[Callable[[], object]] = None,
                 ei: Optional[Callable[[int], object]] = None, tokens=None, clock=time.monotonic) -> None:
        self._on_key = on_key
        self._on_mouse = on_mouse
        self._on_motion = on_motion
        self._connect = connect or portal.JeepneyBus
        self._ei_factory = ei or _ei_receiver
        self._tokens = tokens or portal.TokenStore("input-capture")
        self._clock = clock
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._failure: Optional[BaseException] = None
        self._predicate: Optional[Callable[[], bool]] = None
        self._models: Callable[[], list] = lambda: []
        self.on_home: Optional[Callable[[], None]] = None
        self.on_failure: Optional[Callable[[BaseException], None]] = None
        self._session: Optional[_Session] = None
        self._beat = clock()
        self._reset()

    def _reset(self) -> None:
        self._activation: Optional[int] = None
        self._press: Optional[Press] = None
        self._away = False
        self._home_since: Optional[float] = None
        self._early: List[tuple] = []
        self._early_at = 0.0
        self._walls: Dict[int, str] = {}
        self._placed: List[Tuple[str, Tuple[int, int, int, int]]] = []
        self._sequences: Dict[object, int] = {}  # libei device: the sequence it emulates under
        self._disabled_at: Optional[float] = None
        self._sent_home_at: Optional[float] = None
        self._key = None
        self._zones: List[crossing.Rect] = []
        self._keymap = None
        self._mods = (0, 0, 0, 0)
        self._names_down: Dict[Tuple[int, int], str] = {}
        self._carry = [0.0, 0.0]
        self._scroll_carry = [0.0, 0.0]
        self._frame: List[tuple] = []

    def grab_while(self, predicate: Callable[[], bool]) -> None:
        """Hold input for as long as `predicate` is true, once the desktop has captured it. Asked on
        the capture thread after every batch, so cheap and never blocking."""
        self._predicate = predicate

    def barriers_from(self, models: Callable[[], list]) -> None:
        self._models = models

    @property
    def grabbed(self) -> bool:
        return self._activation is not None

    @property
    def holding(self) -> bool:
        """Whether the desktop has captured input for Beamer now: only then can it go away."""
        return self._activation is not None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            if self._stopping.is_set():
                raise RuntimeError("The last capture thread has not stopped yet")
            return
        self._ready.clear()
        self._stopping.clear()
        self._failure = None
        self._thread = threading.Thread(target=self._run, name="Beamer-hooks", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("The input capture did not start within five seconds")
        if self._failure is not None:
            raise self._failure

    def stop(self) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=2.0)
        if thread.is_alive():
            LOGGER.error("The capture thread did not stop within two seconds; ending its connection")
            self._abort()
            thread.join(timeout=1.0)
        if not thread.is_alive():
            self._thread = None

    def _abort(self) -> None:
        session = self._session
        if session is not None:
            abort = getattr(session.portal.bus, "abort", None)
            if abort is not None:
                abort()

    # -- the thread ----------------------------------------------------------

    def _run(self) -> None:
        session = None
        try:
            session = self._session = _Session(self._connect, self._ei_factory, self._tokens, self._stopping.is_set)
            session.check()
            self._beat = self._clock()
            threading.Thread(target=self._watch, name="Beamer-hooks-watch", daemon=True).start()
            self._ready.set()
            session.open()
            self._set_zones(session.zones())
            while not self._stopping.is_set():
                self._beat = self._clock()
                self._wait(session)
                self._beat = self._clock()
                for path, interface, member, body in session.portal.signals():
                    if interface == INTERFACE and body and body[0] == session.handle:
                        self._signal(member, body[1] if len(body) > 1 else {})
                    elif interface == portal.SESSION and member == "Closed" and path == session.handle:
                        raise CaptureUnavailable(ENDED.format(detail="the session was closed"))
                events = session.ei.events()
                for event in events:
                    self._beat = self._clock()
                    try:
                        self._ei_event(event)
                    except CaptureUnavailable:
                        raise
                    except Exception:
                        LOGGER.exception("An input event could not be read; carrying on")
                self._tick()
        except Exception as exc:
            if not self._stopping.is_set():
                LOGGER.exception("Keyboard and mouse capture stopped")
            started = self._ready.is_set()
            self._failure = exc if not started else None
            self._ready.set()
            if started and not self._stopping.is_set() and self.on_failure is not None:
                try:
                    self.on_failure(exc)
                except Exception:
                    LOGGER.exception("Could not report that capture stopped")
        finally:
            self._end(session)
            self._stopping.set()

    def _end(self, session) -> None:
        """The way home on every exit: let go of a capture, then end the connection, which closes
        the session even if the Release could not be sent."""
        try:
            if session is not None and self._activation is not None:
                session.release(self._activation, desktop_portal.cursor_position())
        except Exception:
            LOGGER.exception("Could not let go of the captured input; closing the session lets go of it")
        finally:
            self._activation = None
            self._names_down.clear()
            desktop_portal.set_captured(False)
            if session is not None:
                try:
                    session.close()
                except Exception:
                    LOGGER.exception("The capture session did not close cleanly")
            self._session = None
            self._reset()

    def _wait(self, session) -> None:
        if session.portal.pending():
            return
        try:
            select.select([session.portal.fileno(), session.ei.fileno()], [], [], TICK_SECONDS)
        except (OSError, ValueError) as exc:
            raise CaptureUnavailable(ENDED.format(detail=exc)) from exc

    def _watch(self) -> None:
        """The safety net under a capture thread stuck while the desktop holds input for it:
        nothing else can let go, so its connection is ended and the portal closes the session."""
        while not self._stopping.wait(STALL_SECONDS / 8):
            if self._activation is not None and self._clock() - self._beat > STALL_SECONDS:
                LOGGER.error("The capture thread has not answered for %s s while the keyboard and mouse are captured; "
                             "ending its connection to free them", STALL_SECONDS)
                self._abort()
                return

    # -- portal signals --------------------------------------------------------

    def _signal(self, member: str, options: dict) -> None:
        if member == "Activated":
            self._activated(options)
        elif member in ("Deactivated", "Disabled"):
            point = options.get("cursor_position")
            was_away = self._away
            self._activation = None
            self._press = None
            self._away = False
            self._frame = []
            desktop_portal.set_captured(False)
            if point is not None:
                desktop_portal.note_position(*point)
            if member == "Disabled":
                self._session.enabled = False
                self._key = None  # set the barriers and enable again, after DISABLED_SECONDS
                self._disabled_at = self._clock()
            # Asked of the sender too: a tray switch may have sent input away a moment ago.
            if was_away or self._away_wanted():
                LOGGER.info("The desktop ended the capture while input was away; bringing it home")
                self._home()
        elif member == "ZonesChanged":
            self._set_zones(self._session.zones())

    def _activated(self, options: dict) -> None:
        activation = options.get("activation_id", NO_ID)
        point = options.get("cursor_position") or desktop_portal.cursor_position()
        barrier = options.get("barrier_id")
        # 0 is a push that met two barriers at once (a corner's two walls); the pointer says which.
        wall = self._walls.get(barrier) if barrier else self._wall_at(point)
        self._activation = activation
        self._frame = []
        desktop_portal.set_captured(True)
        if wall is None:
            LOGGER.info("Captured at a barrier Beamer does not know; letting go")
            self._let_go(desktop_portal.clamp_to(self._zones, *point))
            return
        self._press = Press(wall, point, self._zones, self._clock)
        desktop_portal.note_position(*self._press.point)
        early, self._early = self._early, []
        for event, sequence in early:
            if self._current(sequence):
                self._ei_event(event)

    def _wall_at(self, point) -> Optional[str]:
        """The wall of the placed barrier nearest the pointer, within WALL_PX: the desktop reports
        the pointer on the screen's side of a barrier, or past it by the push."""
        x, y = point
        best = None
        for index, (wall, (x1, y1, x2, y2)) in enumerate(self._placed):
            if index + 1 not in self._walls:
                continue
            distance = max(x1 - x, 0, x - x2) + max(y1 - y, 0, y - y2)
            if distance <= WALL_PX and (best is None or distance < best[0]):
                best = (distance, wall)
        return best[1] if best else None

    def _let_go_unannounced(self) -> None:
        """The desktop started sending input for a capture it never announced (no Activated):
        after EARLY_SECONDS it is let go under its libei sequence, which is its activation id."""
        if not self._early or self._clock() - self._early_at <= EARLY_SECONDS:
            return
        sequence = self._early[-1][1]
        self._early = []
        LOGGER.warning("The desktop captured input without saying so; letting go")
        self._activation = sequence
        self._let_go(desktop_portal.cursor_position())

    def _current(self, sequence: Optional[int]) -> bool:
        """Whether input emulated under `sequence` belongs to the capture now held: libei's start
        sequence is the portal's activation id."""
        return self._activation == NO_ID or sequence is None or sequence == self._activation

    def _home(self) -> None:
        if self.on_home is not None:
            self.on_home()

    def _let_go(self, point: Tuple[int, int]) -> None:
        """Release at `point`. Input counts as held until the Release has gone, and a Release that
        fails ends the session, whose closing lets go of input whatever else has failed."""
        activation = self._activation
        self._press = None
        self._away = False
        self._home_since = None
        self._frame = []
        desktop_portal.note_position(*point)
        try:
            if activation is not None and self._session is not None:
                self._session.release(activation, point)
        except Exception as exc:
            raise CaptureUnavailable(ENDED.format(detail=f"letting go failed: {exc}")) from exc
        finally:
            self._activation = None
            self._names_down.clear()
            desktop_portal.set_captured(False)
        # The sender may have landed the pointer while the Release was on its way.
        landed = desktop_portal.position()
        if landed is not None and landed != desktop_portal.clamp_to(self._zones, *point):
            desktop_portal.set_cursor_position(*landed)

    # -- the tick --------------------------------------------------------------

    def _away_wanted(self) -> bool:
        predicate = self._predicate
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:
            LOGGER.exception("Could not tell whether input is away; bringing it home")
            return False

    def _tick(self) -> None:
        session = self._session
        if self._activation is None:
            # Input away with nothing captured (a switch that passed input_held() just before a
            # let-go): no key can reach the peer, so it comes home.
            if self._away_wanted():
                now = self._clock()
                if self._sent_home_at is None or now - self._sent_home_at >= HOME_AGAIN_SECONDS:
                    if self._sent_home_at is None:
                        LOGGER.info("Input went away with nothing captured; bringing it home")
                    self._sent_home_at = now
                    self._home()
            else:
                self._sent_home_at = None
            self._let_go_unannounced()
            if self._disabled_at is None or self._clock() - self._disabled_at >= DISABLED_SECONDS:
                self._place_barriers(session)
            return
        if self._press is not None:
            if self._away_wanted():
                self._press = None
                self._away = True
            elif self._press.idle() or not self._models():
                self._let_go(self._press.point)
            return
        if self._away_wanted():
            self._home_since = None
            return
        now = self._clock()
        if self._home_since is None:
            self._home_since = now
            return
        if now - self._home_since >= LANDING_SECONDS:
            self._let_go(desktop_portal.cursor_position())

    def _set_zones(self, zones: List[crossing.Rect]) -> None:
        self._zones = zones
        desktop_portal.set_zones(zones)
        self._key = None

    def _place_barriers(self, session) -> None:
        try:
            models = list(self._models())
        except Exception:
            LOGGER.exception("Could not read the zones; no barriers until the next change")
            models = []
        key = (model_key(models), tuple(self._zones))
        if key == self._key:
            if models and not session.enabled:
                session.enable()
            return
        self._key = key
        placed = self._placed = barriers(models, self._zones)
        failed = session.set_barriers([position for _wall, position in placed])
        self._walls = {index + 1: wall for index, (wall, _position) in enumerate(placed) if index + 1 not in failed}
        if failed:
            LOGGER.warning("The desktop refused %d of %d edge barriers", len(failed), len(placed))

    # -- ei events -------------------------------------------------------------

    def _ei_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "device_added":
            device = event[1]
            if hasattr(device, "keymap") and 4 in getattr(device, "caps", ()):  # libei.CAP_KEYBOARD
                self._read_keymap(device)
            return
        if kind == "disconnect":
            raise CaptureUnavailable(ENDED.format(detail="its input connection closed"))
        if kind == "device_paused":
            device = event[1]
            for device_id, code in tuple(self._names_down):
                if device_id == id(device):
                    self._key_event(code, False, device)
            return
        if kind == "modifiers":
            self._mods = tuple(event[2:6])
            return
        if kind == "start":
            self._sequences[event[1]] = event[2]
            return
        if kind == "stop":
            # What arrives between a let-go and the desktop ending the capture belongs to nobody.
            self._sequences.pop(event[1], None)
            if not self._sequences:
                self._early = []
            return
        if kind not in ("motion", "button", "key", "scroll", "scroll_discrete", "frame"):
            return
        sequence = self._sequences.get(event[1]) if len(event) > 1 else None
        if self._activation is None:
            # Ahead of the Activated signal that says which barrier.
            if sequence is not None and len(self._early) < EARLY_MAX:
                if not self._early:
                    self._early_at = self._clock()
                self._early.append((event, sequence))
            return
        if not self._current(sequence):
            if sequence > self._activation and len(self._early) < EARLY_MAX:
                # The next capture's, ahead of its Activated signal.
                if not self._early:
                    self._early_at = self._clock()
                self._early.append((event, sequence))
            return  # else an earlier capture's
        if self._press is not None and self._away_wanted():
            # Sent away from another thread since the last look (the tray, a driven machine).
            self._press = None
            self._away = True
        if self._press is not None:
            self._pressing(event)
            return
        if kind == "frame":
            self._end_frame()
        else:
            self._frame.append(event)

    def _read_keymap(self, device) -> None:
        try:
            text = device.keymap()
            if text:
                import xkb_x11

                if self._keymap is not None:
                    self._keymap.close()
                self._keymap = xkb_x11.Keymap.from_text(text)
        except Exception:
            LOGGER.exception("Could not read the captured keyboard's layout; keys with no fixed name are dropped")

    def _pressing(self, event: tuple) -> None:
        kind = event[0]
        if kind in ("key", "button"):
            if event[3]:
                LOGGER.info("A key or button at the edge lets go of the pointer; it does not reach an app")
                self._let_go(self._press.point)
            return
        if kind != "motion":
            return
        press = self._press
        if not press.feed(event[2], event[3]):
            self._let_go(press.point)
            return
        desktop_portal.note_position(*press.point)
        self._motion(event[2], event[3])
        if self._away_wanted():
            self._press = None
            self._away = True

    def _end_frame(self) -> None:
        events, self._frame = self._frame, []
        discrete = any(event[0] == "scroll_discrete" for event in events)
        for event in events:
            kind = event[0]
            if kind == "motion":
                self._motion(event[2], event[3])
            elif kind == "key":
                self._key_event(event[2], event[3], event[1])
            elif kind == "button":
                self._button(event[2], event[3])
            elif kind == "scroll_discrete":
                self._wheel(event[2] / 120.0 * WHEEL_DELTA, event[3] / 120.0 * WHEEL_DELTA)
            elif kind == "scroll" and not discrete:
                self._wheel(event[2] / PIXELS_PER_NOTCH * WHEEL_DELTA, event[3] / PIXELS_PER_NOTCH * WHEEL_DELTA)

    def _motion(self, dx: float, dy: float) -> None:
        carry = self._carry
        carry[0] += dx
        carry[1] += dy
        whole_x, whole_y = int(carry[0]), int(carry[1])
        carry[0] -= whole_x
        carry[1] -= whole_y
        if whole_x or whole_y:
            self._on_motion(whole_x, whole_y)

    def _wheel(self, units_x: float, units_y: float) -> None:
        """libei scrolls positive down and right; Windows' wheel is positive up, its horizontal
        wheel positive right."""
        carry = self._scroll_carry
        carry[0] += units_x
        carry[1] -= units_y
        for index, message in ((1, WM_MOUSEWHEEL), (0, WM_MOUSEHWHEEL)):
            whole = int(carry[index])
            carry[index] -= whole
            if whole:
                self._on_mouse(message, 0, 0, _wheel_data(whole))

    def _button(self, code: int, down: bool) -> None:
        fixed = _BUTTONS.get(EVDEV_BUTTONS.get(code))
        if fixed is None:
            return
        press, release, data = fixed
        # An answer of False, the button kept here, cannot be played back on Wayland.
        self._on_mouse(press if down else release, 0, 0, data)

    def _key_event(self, code: int, down: bool, device) -> None:
        key = (id(device), code)
        name = self._names_down.get(key) if down else self._names_down.pop(key, None)
        if name is None:
            name = VK_TO_NAME.get(code)
        if name is None and self._keymap is not None:
            depressed, latched, locked, group = self._mods
            mods = self._keymap.core_mods(depressed | latched | locked)
            for held in self._names_down:
                held_code = held[1] if isinstance(held, tuple) else held
                mods |= HELD_MODS.get(held_code, 0)
            name = character(code + KEYCODE_OFFSET, mods, group, self._keymap)
        if name is None:
            return
        if down:
            self._names_down[key] = name
        self._on_key(name, down, code, keytable.EVDEV_US.get(code))


def _ei_receiver(fd: int):
    import libei

    return libei.Context(fd, sender=False)


def _libei_present() -> None:
    import libei

    libei._load()


# Checked before the session is asked for, so the desktop never asks for something Beamer cannot use.
_ei_receiver.preflight = _libei_present
