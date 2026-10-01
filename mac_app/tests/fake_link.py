"""A stand-in for core.link.OutboundLink, so the controller's own behaviour (routing through the
owner, the pointer, the alerts, the clipboard) is tested without a socket.

    controller = KVMController(cfg, link_factory=FakeLink, ...)
    peer = bring_up(controller)           # the primary peer's handshake done: owner.link_up, announcements
    controller.set_redirecting(True)
    peer.sent                             # input the controller gave the link, in order
    peer.posted                           # control messages: focus, clipboard, arrangement, paired, ...

The wire itself (handshake, framing, acks, liveness) is core/tests/test_link.py's; the two ends
together are test_mac_links.py's."""

import threading

from bridge_fakes import settle
from core import protocol

PEER_ID = bytes(range(1, 17))
OTHER_ID = bytes(range(41, 57))
CAPS = ("clipboard", "clipboard_image", "gestures", "media_keys", "text", "settings")


class FakeLink:
    def __init__(self, token, book, identity, *, state=None, up=None, message=None, notice=None, socket_factory=None,
                 tunnel=None, clock=None, reconnect_seconds=2.0, hardware=None, large=None):
        self.token = token
        self.hardware = hardware
        self.large = large
        self.book = book
        self.callbacks = {"state": state, "up": up, "message": message, "notice": notice}
        self.tunnel = tunnel
        self.started = False
        self.stopped = False
        self.peer_id = ""
        self.peer_platform = "windows"
        self.peer_name = ""
        self.caps = frozenset()
        self.via_tunnel = False
        self.kind = "failed"
        self.status = ""
        self.peer_locked = False
        self.dialled = None
        self._sent = []
        self._posted = []
        self.dropped = []
        self.followed = []
        self.samples = []
        self.full = False
        self._live = False
        self._controller = None

    # A take's clipboard is read on a thread of the controller's own, and what follows it for this
    # link waits behind it: a test reading what was sent first waits for that thread to finish.

    @property
    def sent(self):
        self._settle()
        return self._sent

    @property
    def posted(self):
        self._settle()
        return self._posted

    def _settle(self):
        if self._controller is not None and threading.current_thread().name != "Beamer-clipboard":
            settle(self._controller)

    def start(self):
        self.started = True

    def stop(self, wait=True):
        self.stopped = True
        self.stop_waited = wait
        self._live = False

    def refresh(self):
        pass

    def flush(self, timeout=0.5):
        pass

    def drop(self, reason):
        self.dropped.append(reason)

    def live(self):
        return self._live

    def entry(self):
        for peer in self.book.peers():
            if peer.get("token") == self.token:
                return peer
        return None

    def post(self, message):
        if not self._live or self.full:
            return False
        self._posted.append(message)
        return True

    def send_input(self, message):
        if not self._live or self.full:
            return False
        self._sent.append(message)
        return True

    def round_trips(self, max_age=30.0):
        return list(self.samples)

    def follow(self, machines):
        self.followed.append(list(machines))

    # What a test does to it

    def up(self, controller, ident=PEER_ID, name="Office PC", platform="windows", caps=CAPS, accepts=True, hw=None):
        """The handshake done, as the link would report it."""
        self._live = True
        self._controller = controller
        self.peer_id = protocol.id_text(ident)
        self.peer_name, self.peer_platform = name, platform
        self.caps = frozenset(caps)
        self.kind = "connected"
        self.status = f"Connected to {name}"
        self.dialled = ("192.0.2.10", 24820)
        fields = {"id": ident, "name": name, "platform": platform, "app": "1.5.0", "caps": list(caps), "accepts": accepts, "hw": hw}
        controller._link_state(self, True, self.status)
        controller._link_up(self, fields)
        return fields

    def down(self, controller, text="Office PC stopped responding", kind="stopped"):
        self._live = False
        self.kind, self.status = kind, text
        controller._link_state(self, False, text)

    def arrives(self, controller, message, began=None):
        """A message from the peer, as the link hands it on."""
        message = protocol.Message(message)
        if began is not None:
            message.began_at = began
        controller._link_message(self, message)
        settle(controller)

    def types(self):
        return [message["type"] for message in self.posted]


def bring_up(controller, **fields):
    """The controller's primary link, up."""
    link = controller._primary_link
    link.up(controller, **fields)
    return link
