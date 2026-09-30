"""The other direction: the PC's keyboard and mouse arriving on this Mac.

A listener on the same port number this Mac reaches Windows on -- different
machines, so one number does for both -- running the same receiver.py the PC
runs, with this Mac's own injector, clipboard and display geometry handed to
it. Nothing about the protocol or the crossing is duplicated here; this module
is only the wiring that says which platform's modules to use and what to do
when input arrives.

The two directions never overlap. Every focus message is passed to the
controller, which stops its own tap from reading a pointer the PC is moving as
a push against this Mac's edge.
"""

import logging
import threading

import clipboard_mac
import desktop_mac
import input_injector_mac
import no_unlock
from core import receiver
from core import return_edge
from core.receiver import ReceiverServer, ServerState

LOGGER = logging.getLogger("Beamer")


class WindowsInput:
    """Owns the listener and keeps it in step with the settings. `sync` is
    called from the status tick, so a token or port changed in the window
    takes effect without anyone restarting Beamer."""

    def __init__(self, controller, logger=None, arrangement_callback=None, pressure_callback=None, arrival_callback=None):
        self._controller = controller
        self._logger = logger or LOGGER
        # Windows can change the arrangement too, and it reaches this Mac over
        # whichever of the two links is up: this one, or the Mac's own, which
        # bridge.py handles. Both end at the same handler in the app.
        self._arrangement_callback = arrangement_callback
        self._lock = threading.RLock()
        self._running_for = None
        self._ways = None
        self.state = ServerState.STOPPED
        self.detail = "Not listening"
        self.server = ReceiverServer(
            status_callback=self._on_status,
            clipboard=clipboard_mac,
            unlock=no_unlock,
            desktop=desktop_mac,
            injector=input_injector_mac,
            focus_callback=self._on_focus,
            arrangement_callback=self._on_arrangement,
            # The crossing effects: the push home through the return edge, and the PC's pointer landing
            # here. Both fire on the receiver's session thread; the app marshals them.
            pressure_callback=pressure_callback,
            arrival_callback=arrival_callback,
            self_name="Mac",
            peer_name="PC",
            self_target="mac",
            peer_target="windows",
        )
        controller.send_peer_home = self.server.send_home
        # Pause crossing and the full-screen hold are about this screen: the PC's pointer does not
        # go home through a held edge either.
        self.server.edges_held = lambda: controller.crossing_paused or controller.full_screen_app is not None
        self.server.return_model = self._return_model

    def _return_model(self, edge, resistance):
        """The way home for the PC's pointer through `edge` of this Mac, from this Mac's own ways
        in, as for its own pointer: only the chosen thirds, the whole edge, only the notch's own
        stretch of the top edge, the corner when it sits on that edge, else none. On the receiver's
        session thread."""
        crossing_cfg = self._controller.cfg.crossing
        methods = set(crossing_cfg["methods"])
        if "part" in methods:
            return return_edge.PartEdge(edge, crossing_cfg["edge_parts"], resistance)
        if "edge" in methods:
            return return_edge.ReturnEdge(edge, resistance)
        # The same measured range the Mac's own pointer crosses by, read at each push: unmeasured,
        # the notch is a wall, as it is for the Mac's own pointer.
        if "notch" in methods and edge == "top":
            return return_edge.SpanEdge(edge, lambda: self._controller.notch_range, resistance)
        corner = crossing_cfg["corner"]
        if "corner" in methods and edge in corner.split("_"):
            return return_edge.CornerPush(corner, edge, resistance)
        return None

    @property
    def receiving(self) -> bool:
        return bool(self._controller.receiving)

    def sync(self, cfg) -> None:
        speeds = (cfg.pointer_speed, cfg.scroll_speed, cfg.reverse_scroll)
        scale = self.server.input_scale
        if scale is None or (scale.pointer, scale.scroll, scale.reverse) != speeds:
            self.server.input_scale = receiver.InputScale(*speeds)
        # A change on the Crossing page reaches the PC's pointer while it is here, not at its next
        # crossing: the way home is built from these when the PC's input arrives.
        ways = tuple((key, str(cfg.crossing.get(key))) for key in ("methods", "edge_parts", "corner"))
        if ways != self._ways:
            self._ways = ways
            self.server.rearm_return()
        if not cfg.allow_windows_to_drive:
            # Stopping hands back whatever the PC was driving, the same as a dropped link.
            with self._lock:
                if self._running_for is None:
                    return
                self._running_for = None
            self.server.stop()
            return
        wanted = (cfg.port, cfg.auth_token)
        with self._lock:
            if wanted == self._running_for and self.server.listening:
                return
            if not cfg.auth_token or not cfg.port:
                return
            if self._running_for is not None:
                self.server.stop()
            self._running_for = wanted
        try:
            self.server.start(cfg)
        except Exception:
            self._logger.exception("The listener for the PC's input could not start")

    def stop(self) -> None:
        with self._lock:
            self._running_for = None
        self.server.stop()
        self._controller.set_receiving(False)
        try:
            input_injector_mac.release_all()
        except Exception:
            self._logger.exception("Could not release the keys the PC was holding")

    def send_arrangement(self, mac_edge: str, set_at: int) -> bool:
        """Tell the PC over the link it opened to this Mac. Returns False when
        there is nobody connected, which is not a failure worth reporting.
        The hello names this Mac's way home but carries no stamp, so a change
        made while nobody was connected reaches the PC only as that."""
        return self.server.send_arrangement(mac_edge, int(set_at))

    def _on_arrangement(self, mac_edge, set_at) -> None:
        if self._arrangement_callback is None:
            return
        self._arrangement_callback(mac_edge, set_at)

    def _on_status(self, state, detail) -> None:
        self.state = state
        self.detail = detail

    def _on_focus(self, target) -> None:
        # The receiver itself releases whatever the PC was holding before
        # this fires, on both machines alike.
        self._controller.set_receiving(target == "mac")
