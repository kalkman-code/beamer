"""A LinkSender with no sockets: the links are a fake that records what is sent to each peer, the
settings are the shared harness's, and the outbound worker is run by hand (`flush`), so a test reads
what one event did and in what order."""

import os
import sys
import threading
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app_config import Config
from core import protocol, receiver
from core.return_edge import Rect
from core.tests import responder_harness as harness
import sender

HERE, B, C = harness.HERE, harness.B, harness.C
MONITORS = [Rect(0, 0, 1920, 1080)]
CAPS = frozenset({"clipboard", "clipboard_image", "settings", "media_keys"})


class FakeClipboard(harness.FakeClipboard):
    def __init__(self, text="here"):
        super().__init__(text)
        self.get_started = threading.Event()
        self.release_get = threading.Event()
        self.set_started = threading.Event()
        self.release_set = threading.Event()
        self.block_get = False
        self.block_set = False

    def get_contents(self):
        self.get_started.set()
        if self.block_get:
            self.release_get.wait(3)
        return super().get_contents()

    def set_contents(self, text, image):
        self.set_started.set()
        if self.block_set:
            self.release_set.wait(3)
        return super().set_contents(text, image)


def make_config(**overrides):
    values = dict(
        host="", port=24820, auth_token="", machine_id=protocol.id_text(HERE), name="This PC",
        crossing_resistance_px=40,
    )
    values.update(overrides)
    return Config(**values)


def edge_zone(peer, **fields):
    return dict({"peer": protocol.id_text(peer), "kind": "edge"}, **fields)


class FakeLinks:
    """core.link's side of the seam: what the sender asks of its links and what each answers."""

    def __init__(self, owner) -> None:
        self.owner = owner
        self.up_set = set()
        self.refusing = set()
        self.caps_of = {}
        self.sent = []          # (peer, message), input and control in the order they were given
        self.input = []
        self.closed = []
        self.synced = []
        self.fail_input = False
        self.trip = None

    def start(self):
        pass

    def stop(self):
        pass

    def sync(self, entries):
        self.synced.append(list(entries))

    def key_of(self, entry):
        return entry.get("id") or entry.get("token")

    def up(self, peer):
        return peer in self.up_set

    live = up

    def refused(self, peer):
        return peer in self.refusing

    def caps(self, peer):
        return self.caps_of.get(peer, CAPS)

    def post(self, peer, message):
        if peer not in self.up_set:
            return False
        self.sent.append((peer, message))
        return True

    def send_input(self, peer, message):
        if peer not in self.up_set or self.fail_input:
            return False
        self.input.append((peer, message))
        self.sent.append((peer, message))
        return True

    def close(self, peer, reason=None):
        self.closed.append(peer)
        self.up_set.discard(peer)

    def round_trip_ms(self, peer):
        return self.trip

    def follow(self, machines):
        pass


class Rig:
    def __init__(self, cursor=(0, 500), monitors=MONITORS, entries=None, zones=None, up=(B,), **config):
        self.desktop = harness.FakeDesktop(monitors, cursor)
        self.clipboard = FakeClipboard("here")
        self.settings = harness.Settings(
            entries if entries is not None else [harness.entry(B, "Mac", side="left")],
            zones if zones is not None else [edge_zone(B)],
        )
        self.book = receiver.PeerBook(self.settings.load, self.settings.save)
        self.alerts, self.arrivals, self.pressure, self.redirects, self.statuses = [], [], [], [], []
        self.driven = False
        self.sent_home = []
        self.sender = sender.LinkSender(
            self.book,
            lambda: {},
            zones=lambda: self.settings.data["zones"],
            desktop=self.desktop,
            clipboard=self.clipboard,
            links=FakeLinks,
            is_local=lambda host: False,
            status_callback=lambda connected, detail: self.statuses.append((connected, detail)),
            redirect_callback=self.redirects.append,
            pressure_callback=lambda edge, pressure, crossed, part: self.pressure.append((edge, pressure, crossed, part)),
            arrival_callback=lambda edge, x, y: self.arrivals.append((edge, x, y)),
        )
        self.sender.on_alert = lambda title, message: self.alerts.append(message)
        self.sender.driven = lambda: self.driven
        self.sender.send_peer_home = lambda: self.sent_home.append(True) or True
        self.links = self.sender.links
        self.sender.update_config(make_config(**config))
        for peer in up:
            self.bring_up(peer)

    def bring_up(self, peer, accepts=True):
        self.links.up_set.add(peer)
        self.sender.on_link(peer, True, accepts)
        self.flush()

    def drop(self, peer):
        self.links.up_set.discard(peer)
        self.sender.on_link(peer, False)
        self.flush()

    def flush(self):
        """What the outbound worker would have done with everything queued so far."""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            while not self.sender._outbound.empty():
                self.sender._deliver(self.sender._outbound.get_nowait())
            if not self.sender._clip_wait and not self.sender._clip_busy:
                return
            time.sleep(0.002)

    def sent(self, peer=None, kind=None):
        self.flush()
        return [
            message for target, message in self.links.sent
            if (peer is None or target == peer) and (kind is None or message["type"] == kind)
        ]

    def inbound(self, peer, message):
        self.sender.on_message(peer, message)
        self.flush()

    def push(self, times=4, dx=-20, dy=0):
        """The hand still moving with the cursor already stopped at the edge: the position never
        changes, which is why the movement comes from raw input rather than from the hook."""
        for _ in range(times):
            self.sender.on_motion(dx, dy)
        self.flush()

    def accept_take(self, peer=B):
        route = self.sender._owner.route
        self.inbound(peer, protocol.accept_msg(route))
