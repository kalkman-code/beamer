"""Shared test doubles and helpers for the Windows receiver test suite.

receiver.py normally injects real input via input_injector, which only works
on Windows. These helpers let the receiver's connection-handling logic (auth,
versioning, preemption, timeouts, message dispatch) be exercised on any
platform, including this Mac, without touching a real desktop.
"""

import socket
import time
from typing import List, Optional, Tuple
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core import protocol


class FakeInjector:
    """A drop-in replacement for input_injector, passed to
    receiver.handle_message(message, injector=...) so tests never need a real
    Windows desktop (or even ctypes.windll) to run. Records every call so
    tests can assert on what would have been injected."""

    def __init__(self):
        self.calls: List[Tuple[str, tuple]] = []

    def inject_key(self, key, down, us=None):
        self.calls.append(("key", (key, down)))

    def inject_mouse_move(self, dx, dy):
        self.calls.append(("mouse_move", (dx, dy)))

    def inject_mouse_button(self, button, down):
        self.calls.append(("mouse_button", (button, down)))

    def inject_scroll(self, dy, dx=0.0, mode="line"):
        self.calls.append(("scroll", (dy, dx, mode)))

    def inject_gesture(self, name):
        self.calls.append(("gesture", (name,)))


class FakeClipboard:
    """A drop-in replacement for clipboard_win, passed to
    ReceiverServer(status_callback, clipboard=...) so tests never need a real
    Windows desktop (or even ctypes.windll) to run."""

    def __init__(self, text=None, image=None):
        self.text = text
        self.image = image
        self.set_calls: List[Tuple[Optional[str], Optional[bytes]]] = []

    def get_contents(self):
        return self.text, self.image

    def changed_contents(self):
        return self.text, self.image

    def forget_sync(self):
        pass

    def set_contents(self, text, image):
        self.set_calls.append((text, image))
        return True


class FakeDesktop:
    """A drop-in replacement for desktop_win, passed to
    ReceiverServer(status_callback, desktop=...). Monitors are crossing.Rect
    values in virtual-desktop pixels, so a negative-origin arrangement is one
    list away."""

    def __init__(self, monitors, cursor=(0, 0)):
        self._monitors = list(monitors)
        self.cursor = cursor
        self.set_calls: List[Tuple[int, int]] = []

    def monitors(self):
        return list(self._monitors)

    def cursor_position(self):
        return self.cursor

    def set_cursor_position(self, x, y):
        self.cursor = (x, y)
        self.set_calls.append((x, y))


class SecureClient:
    """The Mac's half of a connection, driven from a test over a real
    socket: sends its preamble at once and reads the receiver's on first use,
    so it can be built before the session thread that answers it has started.
    Frames are sealed and opened with the real cipher, never mocked away."""

    def __init__(self, sock, token="shared-token"):
        self.sock = sock
        self.session = protocol.SecureSession(token)
        sock.sendall(self.session.preamble())

    def _preamble(self):
        # Both keys need the receiver's prefix, so neither side of a test can seal or open without it.
        if self.session.peer_prefix is None:
            protocol.recv_preamble(self.sock, self.session)

    def send(self, message):
        self._preamble()
        protocol.send_msg(self.sock, self.session, message)

    def recv(self):
        self._preamble()
        return protocol.recv_msg(self.sock, self.session)

    def settimeout(self, value):
        self.sock.settimeout(value)

    def close(self):
        self.sock.close()


def connected_socket_pair(token="shared-token"):
    """Return (client, server_socket, client_address) for a loopback TCP
    connection, mirroring what ReceiverServer._serve would hand to a session
    thread after accept(). `client` is a SecureClient holding `token`; its raw
    socket is `client.sock` for tests that need to misbehave on purpose."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    client = socket.create_connection(listener.getsockname())
    server, address = listener.accept()
    listener.close()
    return SecureClient(client, token), server, address


def wait_for_status(statuses, state, timeout=1.0):
    """Poll a (state, detail) list appended to by a status callback until an
    entry matching `state` shows up. Status callbacks fire from a background
    session thread, so tests must not assume they've landed the instant a
    socket operation on the other end of the connection completes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in list(statuses):
            if entry[0] == state:
                return entry
        time.sleep(0.01)
    raise AssertionError(f"status {state!r} not observed within {timeout}s; got {statuses!r}")


def wait_for_calls(calls, minimum=1, timeout=1.0):
    """Poll a list a background session thread appends to (e.g.
    FakeClipboard.set_calls) until it has at least `minimum` entries. Needed
    for effects with no reply on the wire to synchronize on, unlike
    wait_for_status which polls status-callback entries instead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(calls) >= minimum:
            return
        time.sleep(0.01)
    raise AssertionError(f"expected at least {minimum} call(s) within {timeout}s; got {calls!r}")
