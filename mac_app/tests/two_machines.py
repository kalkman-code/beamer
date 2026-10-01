"""The Mac's real code at both ends of a loopback pair, for the crossing matrix, the replay tests and the loopback tests.

The Mac is a real KVMController, driven through its event-tap callback with FakeQuartz, over a real
core.link.OutboundLink to `pc_responder`, a core.receiver.LinkResponder on loopback with fakes for
its injector, desktop and clipboard (core/tests/responder_harness.Machine, platform "windows").
That is the Mac driving the PC.

The other direction is the Mac driven: windows_input.WindowsInput wires the Mac's real
LinkResponder to the controller, with the Mac's injector, desktop and clipboard swapped for fakes
(patched into windows_input's module names while it is built, so no product code differs), and
`pc` is a scripted version 6 initiator (responder_harness.Initiator) standing in for the PC's own
link. The PC is one machine with one pairing: both directions run under PAIRED_TOKEN and the
PC's one id. win_app is not involved: the PC's own half is step 5b's.

Settings live in a real SettingsStore in a temporary folder, so the controller's book and the
Mac's responder read what the app would."""

import copy
import os
import socket
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import windows_input
from bridge import KVMController
import bridge_fakes
from bridge_fakes import PAIRED_TOKEN, FakeClock, FakeQuartz, crossing_config, quiet_logger
from core import protocol
from core.return_edge import Rect
from core.tests.responder_harness import HERE, FakeDesktop, FakeInjector, Initiator, Machine, entry
from settings_store import SettingsStore

MAC_BOUNDS = (0, 0, 1728, 1117)
PC_MONITORS = [Rect(0, 0, 1920, 1080)]
OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}
PC_TEXT = protocol.id_text(HERE)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class Duo:
    """`crossing` is the Mac's crossing settings (its own engine); `side` the Mac edge that faces the
    PC as the Mac's book keeps it, which is the crossing's `edge` unless given; `mac_zones` the
    zones the Mac's responder arms for the PC while the PC drives it, and `pc_zones` the PC's for
    the Mac; `pc_side` the PC edge that faces the Mac. All default to the whole edge facing the
    other machine, and `side=""` is a Mac that has learnt no side for the PC. `allow_drive` is the
    Mac's setting for the PC driving it, `side_stamp` and `side_by` how the Mac's book dates its
    side. A zone's `peer` may be written "mac" or "pc" for the ids the store makes. Nothing starts until `listen()` and the `link_*` calls: the Mac's responder listens, the
    Mac's own link to the PC, and the PC's link to the Mac, each can be left down."""

    def __init__(self, crossing=None, mac_cfg=None, side=None, mac_zones=None, pc_zones=None, pc_side=None,
                 mac_bounds=MAC_BOUNDS, pc_monitors=None, notch=None, pc_notch=None, allow_drive=True,
                 side_stamp=0, side_by="", socket_factory=socket.create_connection):
        FakeQuartz.reset_cursor_spies()
        self.clock = FakeClock()
        self.feedback = []
        self.alerts = []
        self.mac_arrangements = []
        self.mac_arrivals = []
        self.mac_bounds = mac_bounds
        self.mac_port = free_port()
        self.dir = tempfile.TemporaryDirectory()
        self.store = SettingsStore(Path(self.dir.name) / "settings.json")
        self.mac_id = protocol.read_id(self.store.current()["machine_id"])
        self.mac_text = protocol.id_text(self.mac_id)

        cfg = mac_cfg or crossing_config(PAIRED_TOKEN, **(crossing or {}))
        cfg.host, cfg.port, cfg.auth_token = "127.0.0.1", self.mac_port, PAIRED_TOKEN
        cfg.reconnect_interval_s = 0.05
        self.side = cfg.crossing["edge"] if side is None else side
        self.pc_side = pc_side if pc_side is not None else OPPOSITE.get(self.side, "")

        self.pc_desktop = FakeDesktop(pc_monitors or PC_MONITORS, cursor=(900, 500))
        self.pc_responder = Machine(
            [entry(self.mac_id, "Mac", platform="macos", token=PAIRED_TOKEN, host="127.0.0.1", port=self.mac_port,
                   side=self.pc_side)],
            self._named(pc_zones if pc_zones is not None else [{"peer": "mac", "kind": "edge"}]),
            desktop=self.pc_desktop, notch_span=(lambda: pc_notch) if pc_notch else None,
        ).start()
        self.pc_injector = self.pc_responder.injector

        settings = copy.deepcopy(self.store.current())
        settings["peers"] = [entry(HERE, "PC", platform="windows", token=PAIRED_TOKEN, host="127.0.0.1",
                                   port=self.pc_responder.port, side=self.side, side_set_at=side_stamp, side_by=side_by,
                                   allow_drive=allow_drive)]
        settings["zones"] = self._named(mac_zones if mac_zones is not None else [{"peer": "pc", "kind": "edge"}])
        self.store.save_settings(settings)

        # The one pasteboard the controller and the Mac's responder share, as on a real Mac.
        self.clipboard = bridge_fakes.FakeClipboard("on the Mac")
        self.mac = KVMController(
            cfg, logger=quiet_logger(), quartz=FakeQuartz, clock=self.clock, desktop_bounds=lambda: self.mac_bounds,
            book=self.store.book(), socket_factory=socket_factory, clipboard=self.clipboard,
        )
        self.mac.on_crossing = lambda kind, step: self.feedback.append((kind, step))
        self.mac.on_user_alert = lambda title, message: self.alerts.append(message)
        self.mac.on_arrangement = lambda edge, set_at, by: self.mac_arrangements.append((edge, set_at, by))
        self.mac.notch_range = notch

        left, top, right, bottom = mac_bounds
        self.mac_desktop = FakeDesktop([Rect(left, top, right - left, bottom - top)], cursor=(800, 500))
        self.mac_injector = FakeInjector()
        with mock.patch.multiple(
            windows_input, input_injector_mac=self.mac_injector, desktop_mac=self.mac_desktop, clipboard_mac=self.clipboard
        ):
            self.mac_input = windows_input.WindowsInput(
                self.mac, logger=quiet_logger(),
                arrangement_callback=lambda edge, set_at, by: self.mac_arrangements.append((edge, set_at, by)),
                arrival_callback=lambda edge, x, y: self.mac_arrivals.append((edge, x, y)),
            )
        self.mac_responder = self.mac_input.server
        self.pc = None
        self.cfg = cfg
        self._started = False
        self._closed = False

    def _named(self, zones):
        """Zones written with `"peer": "mac"` or `"pc"`, which are ids only once the store has made them."""
        names = {"mac": self.mac_text, "pc": PC_TEXT}
        return [{**zone, "peer": names.get(zone["peer"], zone["peer"])} for zone in zones]

    # -- links ---------------------------------------------------------------

    def listen(self):
        """The Mac's responder, as it runs whenever the app is open."""
        self.mac_input.sync(self.cfg)
        assert wait_for(lambda: self.mac_responder.listening), "the Mac's responder never listened"
        assert wait_for(self._accepting), "the Mac's responder never accepted"
        return self

    def _accepting(self):
        try:
            with socket.create_connection(("127.0.0.1", self.mac_port), timeout=0.2):
                return True
        except OSError:
            return False

    def link_mac_to_pc(self):
        """The Mac's own link to the PC, up before this returns."""
        if not self._started:
            self.mac.start()
            self._started = True
        assert wait_for(lambda: self.mac.connected), f"the Mac never connected: {self.mac.connection_status}"
        # `connected` is the link's handshake; the controller knows the peer a beat later.
        assert wait_for(lambda: PC_TEXT in self.mac._peers_up), "the controller never took the PC's link up"
        return self

    def link_pc_to_mac(self, wrap=None):
        """The PC's own link to the Mac, handshaken: `pc` is the scripted initiator. `wrap` is given
        the connected socket before anything is sent and returns the one to use instead."""
        self.pc = Initiator(types.SimpleNamespace(port=self.mac_port), peer=HERE, key=PAIRED_TOKEN, preamble=wrap is None)
        if wrap is not None:
            self.pc.sock = wrap(self.pc.sock)
            self.pc.sock.sendall(self.pc.session.preamble())
        # Its hello says the port it listens on, which the Mac saves as the address to dial.
        self.pc.handshake(self.pc.hello(platform="windows", name="PC", port=self.pc_responder.port))
        assert wait_for(lambda: HERE in self.mac_responder.links()), "the Mac's responder never saw the PC's link"
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.pc is not None:
            # Closing the descriptor does not end a recv another thread is blocked in, so the Mac
            # would hear nothing for its read timeout; a shutdown does.
            try:
                self.pc.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.pc.close()
        self.mac.stop()
        self.mac_responder.stop()
        self.pc_responder.stop()
        self.dir.cleanup()

    # -- the Mac's pointer ----------------------------------------------------

    def mac_move(self, x, y, dx, dy=0, event_type=FakeQuartz.kCGEventMouseMoved):
        event = {
            "location": (x, y),
            FakeQuartz.kCGMouseEventDeltaX: dx,
            FakeQuartz.kCGMouseEventDeltaY: dy,
        }
        self.clock.value += 0.01
        return self.mac._event_tap_callback(None, event_type, event, None)

    def mac_push(self, x, y, dx, dy=0, times=12, until=lambda self: self.mac.redirecting):
        for _ in range(times):
            self.mac_move(x, y, dx, dy)
            if until(self):
                return True
        return False

    def mac_lean(self, dx, dy=0, times=6, pace=0.02):
        """The Mac's forwarded pointer pushing on the PC's edge, one delta at a time with a breath
        between, until the input is home. The location is nothing the PC sees: a redirected
        pointer is pinned and only its deltas travel."""
        for _ in range(times):
            self.mac_move(1727.0, 558.0, dx, dy)
            if not self.mac.redirecting:
                return True
            wait_for(lambda: not self.mac.redirecting, pace)
        return wait_for(lambda: not self.mac.redirecting, 0.5)

    # -- the PC driving the Mac ---------------------------------------------------

    def pc_takes(self, edge=None, offset=None, resistance_px=40, reach=None, stay=False):
        """The scripted PC takes this Mac's input: a `focus` naming the Mac. Returns the route."""
        pc = self.pc
        pc.route += 1
        fields = {"edge": edge, "offset": offset, "resistance_px": resistance_px, "reach": [] if reach is None else reach}
        pc.send(protocol.focus_v6(pc.route, self.mac_id, stay=stay, **fields))
        return pc.route

    def pc_drives(self, edge=None, offset=None, **fields):
        """`pc_takes`, with the answer read and the Mac seen to be driven. Returns the route."""
        route = self.pc_takes(edge, offset, **fields)
        answer = self.pc.answer(route)
        assert answer == (protocol.MSG_ACCEPT, {"route": route}), f"the Mac answered {answer}"
        assert wait_for(lambda: self.mac.receiving), "the Mac never became driven"
        return route

    def pc_leans(self, cursor, dx, dy=0, times=1):
        """The PC's pointer, arriving as input, pushing at the Mac's edge from `cursor`. Each move
        is waited for, so what the Mac's zones did with it has been done when this returns."""
        self.mac_desktop.cursor = cursor
        for _ in range(times):
            seq = self.pc.move(dx, dy)
            assert self.pc.acked(seq) is not None, "the Mac never acknowledged the move"

    def pc_switch(self, timeout=2.0):
        """The `switch` the Mac's responder sent the PC, or None."""
        return self.pc.expect(protocol.MSG_SWITCH, timeout)
