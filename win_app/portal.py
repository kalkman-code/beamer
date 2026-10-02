"""The desktop portals over D-Bus, for the Wayland back ends.

One `Portal` per thread that uses it: the capture thread has its own and the injector's thread has
its own, each on its own connection to the session bus. A connection is the session's life: when
it closes, by a stop or by the process dying, the portal closes every session it opened, and the
compositor lets go of input it had captured. That is the way home that needs nothing to run.

The bus is a seam (`JeepneyBus` for real): the request and response matching here is tested with
a fake one on any machine. jeepney is pure Python and is reached only when a JeepneyBus is made,
so this module imports anywhere. Not QtDBus: it would tie both threads to Qt's event loop, where
the capture thread must select on the bus and libei's fd together, as the X11 one selects on its
X connection.

Portal methods that may ask the user (CreateSession, Start) answer through a Request object's
Response signal: 0 granted, 1 refused by the user, 2 ended another way."""

import collections
import logging
import os
import socket
import time
from pathlib import Path
from typing import Callable, Optional

LOGGER = logging.getLogger(__name__)

BUS_NAME = "org.freedesktop.portal.Desktop"
OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST = "org.freedesktop.portal.Request"
SESSION = "org.freedesktop.portal.Session"
PROPERTIES = "org.freedesktop.DBus.Properties"
CALL_SECONDS = 10.0
TICK_SECONDS = 0.1


class Refused(Exception):
    """The user said no in the desktop's dialog."""


class PortalError(Exception):
    """The portal failed or is missing; the text says which."""


def plain(value):
    """A message body as plain values: jeepney gives each value of an a{sv} as its variant, a
    (signature, value) pair."""
    if isinstance(value, dict):
        return {key: plain(unvariant(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(plain(item) for item in value)
    return value


def unvariant(value):
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return value[1]
    return value


class TokenStore:
    """A portal's restore token, kept between runs in the state directory, readable by this user
    only: it lets this app skip the desktop's question. Each token is used once and replaced by
    the one the next start returns; an empty one is forgotten."""

    def __init__(self, name: str, base: Optional[Path] = None) -> None:
        if base is None:
            configured = os.environ.get("XDG_STATE_HOME", "")
            base = Path(configured) if configured and os.path.isabs(configured) else Path.home() / ".local" / "state"
            base = base / "beamer"
        self.path = Path(base) / f"{name}-token"

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def write(self, token: str) -> None:
        try:
            if not token:
                self.path.unlink(missing_ok=True)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(token)
            os.replace(temporary, self.path)
        except OSError:
            LOGGER.exception("Could not keep the desktop's permission for next time")


class Portal:
    """Calls and requests on the portal over `bus`, keeping every other signal for the owner to
    read with signals(). `stopping()` ends a wait for the user early."""

    def __init__(self, bus, stopping: Callable[[], bool] = lambda: False) -> None:
        self.bus = bus
        self._stopping = stopping
        self._waiting = collections.deque()
        self._count = 0
        self._sender = bus.unique_name.lstrip(":").replace(".", "_")

    def token(self) -> str:
        self._count += 1
        return f"beamer{self._count}"

    def call(self, interface: str, method: str, signature: str = "", body: tuple = (), path: str = OBJECT_PATH):
        return self.bus.call(path, interface, method, signature, body)

    def version(self, interface: str) -> int:
        """The portal interface's version, 0 when the portal does not have it."""
        try:
            return int(unvariant(self.call(PROPERTIES, "Get", "ss", (interface, "version"))[0]))
        except PortalError:
            return 0

    def request(self, interface: str, method: str, signature: str, make_body: Callable[[str], tuple],
                timeout: Optional[float] = None) -> dict:
        """Call a method answered by a Response, wait for it and return its results. `make_body`
        gets the handle_token to put in the options. No timeout by default: a dialog waits for the
        user for as long as it takes, though a stop ends the wait."""
        token = self.token()
        path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"
        self.call(interface, method, signature, make_body(token))
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            for item in list(self._waiting):
                _kind, at, iface, member, body = item
                if at == path and iface == REQUEST and member == "Response":
                    self._waiting.remove(item)
                    code, results = body[0], plain(body[1]) if len(body) > 1 else {}
                    if code == 0:
                        return results
                    if code == 1:
                        raise Refused(f"{method} was refused")
                    raise PortalError(f"{method} ended without an answer ({code})")
            if self._stopping():
                raise PortalError(f"{method} was abandoned: Beamer is stopping")
            wait = TICK_SECONDS
            if deadline is not None:
                wait = min(wait, deadline - time.monotonic())
                if wait <= 0:
                    raise PortalError(f"{method} had no answer")
            self._waiting.extend(self.bus.receive(wait))

    def signals(self, timeout: float = 0.0) -> list:
        """Every signal received since the last call, after waiting up to `timeout` for one, as
        (path, interface, member, body) with the body's variants made plain."""
        if not self._waiting:
            self._waiting.extend(self.bus.receive(timeout))
        found = [(at, iface, member, plain(body)) for _kind, at, iface, member, body in self._waiting]
        self._waiting.clear()
        return found

    def fileno(self) -> int:
        return self.bus.fileno()

    def pending(self) -> bool:
        """Whether signals are waiting, here or already read off the socket by the bus, so a
        select() on its fd would wait for nothing."""
        if not self._waiting:
            pending = getattr(self.bus, "pending", None)
            if pending is not None and pending():
                self._waiting.extend(self.bus.receive(0))
        return bool(self._waiting)

    def close(self) -> None:
        self.bus.close()


class JeepneyBus:
    """The session bus through jeepney's blocking connection, with fd passing. Signals from the
    portal are matched once and kept until receive() hands them over, those that arrive while a
    call waits for its reply included."""

    def __init__(self) -> None:
        try:
            from jeepney.bus_messages import MatchRule, message_bus
            from jeepney.io.blocking import open_dbus_connection
        except ImportError as exc:
            raise PortalError("jeepney is not installed; Beamer needs it to reach the desktop portals") from exc
        try:
            self._conn = open_dbus_connection(bus="SESSION", enable_fds=True)
        except Exception as exc:
            raise PortalError(f"Could not reach the session bus: {exc}") from exc
        self._signals = collections.deque()
        # The bus routes only the portal's signals here; the local filter takes every signal, since
        # a message names its sender by unique name, which a well-known name in it never matches.
        self._filter = self._conn.filter(MatchRule(type="signal"), queue=self._signals)
        self._conn.send_and_get_reply(message_bus.AddMatch(MatchRule(type="signal", sender=BUS_NAME)), timeout=CALL_SECONDS)
        self.unique_name = self._conn.unique_name

    def call(self, path: str, interface: str, method: str, signature: str, body: tuple) -> tuple:
        from jeepney import DBusAddress, MessageType, new_method_call

        message = new_method_call(DBusAddress(path, BUS_NAME, interface), method, signature or None, body)
        try:
            reply = self._conn.send_and_get_reply(message, timeout=CALL_SECONDS)
        except TimeoutError as exc:
            raise PortalError(f"{interface}.{method} did not answer") from exc
        if reply.header.message_type == MessageType.error:
            name = reply.header.fields.get(4, "an error")  # HeaderFields.error_name
            raise PortalError(f"{interface}.{method}: {name}: {' '.join(map(str, reply.body))}")
        return tuple(_raw_fds(reply.body))

    def receive(self, timeout: float) -> list:
        if not self._signals and timeout >= 0:
            try:
                self._conn.recv_messages(timeout=max(timeout, 0.0))
            except TimeoutError:
                pass
        found = []
        while self._signals:
            message = self._signals.popleft()
            fields = message.header.fields
            # HeaderFields: 1 path, 2 interface, 3 member.
            found.append(("signal", fields.get(1), fields.get(2), fields.get(3), tuple(_raw_fds(message.body))))
        return found

    def pending(self) -> bool:
        if not self._signals:
            try:
                self._conn.recv_messages(timeout=0)
            except TimeoutError:
                pass
        return bool(self._signals)

    def fileno(self) -> int:
        return self._conn.sock.fileno()

    def abort(self) -> None:
        """End the connection from another thread: the thread waiting on it wakes, and the portal
        closes this connection's sessions."""
        try:
            self._conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            LOGGER.exception("The portal connection did not close cleanly")


def _raw_fds(body):
    """jeepney hands fds over as FileDescriptor objects that close themselves when dropped; each is
    taken as a plain fd the caller now owns."""
    for item in body:
        to_raw = getattr(item, "to_raw_fd", None)
        yield to_raw() if to_raw is not None else item
