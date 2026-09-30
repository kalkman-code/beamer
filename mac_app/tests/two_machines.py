"""Both apps' real link code against each other over loopback, for the crossing matrix.

The Mac is a real KVMController, driven through its event-tap callback with FakeQuartz; the PC is a
real MacSender, driven through its hook entry points. Each machine also runs the shared
ReceiverServer that the other one's link connects to, as it does in the apps. Only the platform
edges are faked: the pointer, the injector, the clipboard and the display geometry.

win_app's modules are put on the path after this app's, so the PC's own (`sender`, `app_config`)
resolve to the PC's; what both share comes from core.
"""

import os
import socket
import sys
import time

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WIN = os.path.join(os.path.dirname(_APP), "win_app")
for _path in (_WIN, os.path.join(_WIN, "tests")):
    if _path not in sys.path:
        sys.path.append(_path)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol
from core import receiver
from core.return_edge import Rect

import app_config
import sender as pc_sender_module
from bridge import KVMController
from fakes import FakeClipboard, FakeDesktop, FakeInjector
from test_bridge import FakeClock, FakeQuartz, crossing_config, quiet_logger

TOKEN = "matrix-token"
MAC_BOUNDS = (0, 0, 1728, 1117)
PC_MONITORS = [Rect(0, 0, 1920, 1080)]


class NoUnlock:
    def is_locked(self):
        return None


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
    return False


class Duo:
    """`crossing` is the Mac's crossing settings, `pc` the PC's Config fields. The two links are
    started separately: `link_mac_to_pc()` is the Mac's own link to the PC, `link_pc_to_mac()` is
    the PC's link to the Mac, and each can be left down."""

    def __init__(self, crossing=None, pc=None, mac_cfg=None, pc_monitors=None, mac_bounds=MAC_BOUNDS):
        FakeQuartz.reset_cursor_spies()
        self.clock = FakeClock()
        self.feedback = []
        self.mac_bounds = mac_bounds
        self.mac_port = free_port()
        self.pc_port = free_port()
        while self.pc_port == self.mac_port:
            self.pc_port = free_port()

        self.mac_desktop = FakeDesktop([Rect(*self._rect(mac_bounds))], cursor=(800, 500))
        self.pc_desktop = FakeDesktop(pc_monitors or PC_MONITORS, cursor=(900, 500))
        self.mac_injector = FakeInjector()
        self.pc_injector = FakeInjector()
        self.mac_peer = []
        self.pc_peer = []
        self.mac_arrangements = []
        self.pc_arrangements = []

        cfg = mac_cfg or crossing_config(**(crossing or {}))
        cfg.host, cfg.port, cfg.auth_token = "127.0.0.1", self.pc_port, TOKEN
        cfg.reconnect_interval_s = 0.05
        self.mac = KVMController(
            cfg, logger=quiet_logger(), quartz=FakeQuartz, clock=self.clock, desktop_bounds=lambda: self.mac_bounds
        )
        self.mac.on_crossing = lambda kind, step: self.feedback.append((kind, step))
        self.mac.on_user_alert = lambda title, message: None
        self.mac_arrivals = []

        pc_fields = dict(
            host="127.0.0.1",
            port=self.mac_port,
            auth_token=TOKEN,
            mac_host="127.0.0.1",
            mac_return_edge="left",
            crossing_resistance_px=40,
            mac_resistance_px=40,
        )
        pc_fields.update(pc or {})
        self.pc_config = app_config.Config(**pc_fields)
        self.pc = pc_sender_module.MacSender(
            desktop=self.pc_desktop, clipboard=FakeClipboard(), is_local=lambda host: False
        )
        self.pc_arrivals = []
        self.pc._arrival_callback = lambda edge, x, y: self.pc_arrivals.append((edge, x, y))
        self.pc._arrangement_callback = lambda edge, set_at: self.pc_arrangements.append((edge, set_at))
        self.mac.on_arrangement = lambda edge, set_at: self.mac_arrangements.append((edge, set_at))
        self.pc_pressure = []
        self.pc._pressure_callback = lambda edge, pressure, crossed, part: self.pc_pressure.append((edge, crossed))

        # The PC's receiver: what the Mac's link connects to.
        self.pc_server = receiver.ReceiverServer(
            status_callback=lambda state, detail: None,
            clipboard=FakeClipboard(),
            unlock=NoUnlock(),
            desktop=self.pc_desktop,
            injector=self.pc_injector,
            focus_callback=lambda target: self.pc.set_receiving(target == "windows"),
            peer_callback=lambda *args: self.pc_peer.append(args),
            arrangement_callback=lambda edge, set_at: self.pc_arrangements.append((edge, set_at)),
        )
        self.pc.send_peer_home = self.pc_server.send_home
        # The Mac's receiver: what the PC's link connects to.
        self.mac_server = receiver.ReceiverServer(
            status_callback=lambda state, detail: None,
            clipboard=FakeClipboard(),
            unlock=NoUnlock(),
            desktop=self.mac_desktop,
            injector=self.mac_injector,
            focus_callback=lambda target: self.mac.set_receiving(target == "mac"),
            peer_callback=lambda *args: self.mac_peer.append(args),
            arrangement_callback=lambda edge, set_at: self.mac_arrangements.append((edge, set_at)),
            arrival_callback=lambda edge, x, y: self.mac_arrivals.append((edge, x, y)),
            self_name="Mac",
            peer_name="PC",
            self_target="mac",
            peer_target="windows",
        )
        self.mac.send_peer_home = self.mac_server.send_home
        self._started = False

    @staticmethod
    def _rect(bounds):
        left, top, right, bottom = bounds
        return left, top, right - left, bottom - top

    # -- links ---------------------------------------------------------------

    def listen(self):
        """Both receivers, as they run whenever the app is open."""
        self.pc_server.start(app_config.Config(host="127.0.0.1", port=self.pc_port, auth_token=TOKEN))
        from config import Config as MacConfig

        self.mac_server.start(MacConfig(host="127.0.0.1", port=self.mac_port, auth_token=TOKEN))
        return self

    def link_mac_to_pc(self):
        """The Mac's own link to the PC, up before this returns."""
        if not self._started:
            self.mac.start()
            self._started = True
        assert wait_for(lambda: self.mac.connected), f"the Mac never connected: {self.mac.connection_status}"
        return self

    def link_pc_to_mac(self):
        self.pc.start(self.pc_config)
        assert wait_for(lambda: self.pc.connected), f"the PC never connected: {self.pc.status}"
        return self

    def close(self):
        self.pc.stop()
        self.mac.stop()
        self.pc_server.stop()
        self.mac_server.stop()

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

    # -- the PC's pointer ----------------------------------------------------

    def pc_push(self, x, y, dx, dy=0, times=12, until=lambda self: self.pc.redirecting):
        self.pc_desktop.cursor = (x, y)
        for _ in range(times):
            self.pc.on_motion(dx, dy)
            if until(self):
                return True
        return False
