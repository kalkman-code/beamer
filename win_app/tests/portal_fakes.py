"""Fakes for the Wayland back ends' seams: a session bus with a scripted desktop portal on it, and a
libei context the test feeds events into. Neither needs D-Bus, libei or Qt, so the portal modules'
tests run on every machine."""

import os
import threading
import time

import portal


def variant(value):
    """A value as jeepney hands over each entry of an a{sv}: (signature, value)."""
    return ("v", value)


def vardict(values: dict) -> dict:
    return {key: variant(value) for key, value in values.items()}


class FakeBus:
    """The portal as a script: `answers[method]` is (code, results) for a method answered by a
    Response, or a callable taking the body and returning one; `returns[method]` is the body a
    plain call returns. Every call is recorded in `calls` as (interface, method, body)."""

    REQUESTS = {"CreateSession", "SelectDevices", "Start", "GetZones", "SetPointerBarriers"}

    def __init__(self, versions=None) -> None:
        self.unique_name = ":1.42"
        self.versions = versions or {}
        self.failures = {}
        self.answers = {}
        self.returns = {}
        self.calls = []
        self.closed = False
        self.aborted = False
        self._signals = []
        self._lock = threading.Lock()
        self._read, self._write = os.pipe()

    def call(self, path, interface, method, signature, body):
        if self.closed:
            raise portal.PortalError("the connection is closed")
        with self._lock:
            self.calls.append((interface, method, body))
        if method in self.failures:
            raise self.failures[method]
        if interface == portal.PROPERTIES and method == "Get":
            version = self.versions.get(body[0])
            if version is None:
                raise portal.PortalError("No such interface")
            return (("u", version),)
        if method in self.REQUESTS:
            options = next(item for item in body if isinstance(item, dict))
            token = options["handle_token"][1]
            request = f"/org/freedesktop/portal/desktop/request/1_42/{token}"
            answer = self.answers.get(method, (0, {}))
            if callable(answer):
                answer = answer(body)
            if answer is not None:
                code, results = answer
                self.emit(request, portal.REQUEST, "Response", (code, vardict(results)))
            return (request,)
        returned = self.returns.get(method, ())
        return returned(body) if callable(returned) else returned

    def emit(self, path, interface, member, body) -> None:
        with self._lock:
            self._signals.append(("signal", path, interface, member, body))
        os.write(self._write, b"x")

    def receive(self, timeout):
        with self._lock:
            found, self._signals = self._signals, []
        if not found and timeout > 0:
            time.sleep(min(timeout, 0.01))
        return found

    def made(self, method):
        with self._lock:
            return [body for _interface, name, body in self.calls if name == method]

    def fileno(self):
        return self._read

    def abort(self):
        self.aborted = True
        self.closed = True

    def close(self):
        self.closed = True


class FakeDevice:
    def __init__(self, name, caps, regions=(), keymap=None) -> None:
        self.name = name
        self.caps = set(caps)
        self._regions = list(regions)
        self._keymap = keymap

    def has(self, cap):
        return cap in self.caps

    def regions(self):
        return list(self._regions)

    def keymap(self):
        return self._keymap

    def __repr__(self):
        return f"FakeDevice({self.name})"


class FakeEi:
    """A libei context: `feed(*events)` queues events for the next events(); what is sent is in
    `sent`, as (call, device name, *args)."""

    def __init__(self, fd=None) -> None:
        self.fd = fd
        self._events = []
        self._lock = threading.Lock()
        self.sent = []
        self.bound = []
        self.closed = False
        self._read, self._write = os.pipe()

    def feed(self, *events) -> None:
        with self._lock:
            self._events.extend(events)
        os.write(self._write, b"x")

    def events(self):
        with self._lock:
            found, self._events = self._events, []
        return found

    def fileno(self):
        return self._read

    def bind(self, seat):
        self.bound.append(seat)

    def _record(self, call, device, *args):
        with self._lock:
            self.sent.append((call, device.name) + args)

    def start(self, device, sequence):
        self._record("start", device, sequence)

    def stop(self, device):
        self._record("stop", device)

    def frame(self, device):
        self._record("frame", device)

    def motion(self, device, dx, dy):
        self._record("motion", device, dx, dy)

    def absolute(self, device, x, y):
        self._record("absolute", device, x, y)

    def button(self, device, code, down):
        self._record("button", device, code, down)

    def scroll(self, device, dx, dy):
        self._record("scroll", device, dx, dy)

    def scroll_discrete(self, device, dx, dy):
        self._record("scroll_discrete", device, dx, dy)

    def key(self, device, code, down):
        self._record("key", device, code, down)

    def sends(self, call=None):
        with self._lock:
            return [item for item in self.sent if call is None or item[0] == call]

    def close(self):
        self.closed = True


def wait_for(condition, timeout=3.0, what="the condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")
