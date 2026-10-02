"""The owner: where this machine's input goes, as WIRE.md sections 4 and 5 describe it.

Pure, with no sockets, no threads, no platform code and no `time`: the clock is passed in, so the
same machine serves the Mac, Windows, Linux and (ported) the phones, and every routing rule is
tested without a link. The app feeds it events and carries out the actions each one returns, in
order:

- `go(peer, edge, offset)`: this machine's own zone, shortcut or menu picks a peer, or home
  (`None` or this machine's id).
- `switch`, `accept`, `refuse` and `accepts`: those messages' `data` from a peer, already
  validated by the protocol layer.
- `link_up(peer, accepts)` once a link's handshake is done (`accepts` from its `welcome`), and
  `link_down(peer)` when it ends for any reason, its watchdog and a failed send included.
- `input(message)`: every input message this machine captures (`keydown`, `mousemove`, ...),
  without a `seq`; the link adds that.
- `clipboard_changed()` when this machine's clipboard changes by its own hand (never when it is
  set from a peer), and `clipboard_arrived(peer, began_at, message)` for a peer's `clipboard`.
- `tick()` at `deadline()`, or at any time: the one-second waits and the three seconds a peer that
  could not be reached is left out of `reach`.

The actions: `Send` a message on the link this machine opened to a peer; `SendClipboard` (this
machine's clipboard, to that peer); `SetClipboard` (here); `Moved` (the input is now on a peer, or
home, and why, which is what the app captures, lands and says by); `Unreachable` (a hand-over
that did not happen, to say so); `Drop` (close the link this machine opened to a peer and let it
reconnect: a hand-over to it was given up before it answered, and its `accept` may still be on the
way); and `LOCAL` (let this input event through on this machine).
An input event that returns nothing is swallowed or held: the app suppresses it.

`away` is what the responder's `busy` refusal asks: true from the moment a take is sent until it
is refused, times out or is let go.
"""

from dataclasses import dataclass
from typing import Optional

TAKE_WAIT = 1.0         # seconds a take waits for its `accept`
LEFT_OUT = 3.0          # seconds a peer that did not take a hand-over, or refused a take, is not asked again
SENT_HOME_FOR = 5.0     # a peer that sent this machine home refuses it this long (receiver.SENT_HOME_SECONDS)
CLIPBOARD_AFTER = 10.0  # seconds after a let-go that the machine let go may send its clipboard
TAKE_MEMORY = 30.0      # seconds a take is remembered, so a late `accept` can still be let go
REFUSALS = ("owned", "not_allowed", "busy", "sent_home", "malformed")

_DOWNS = {"keydown": "keyup", "mousedown": "mouseup"}
_UPS = ("keyup", "mouseup")


@dataclass(frozen=True)
class Send:
    peer: str
    message: dict


@dataclass(frozen=True)
class SendClipboard:
    peer: str


@dataclass(frozen=True)
class SetClipboard:
    message: dict


@dataclass(frozen=True)
class Moved:
    to: Optional[str]           # None: home
    left: Optional[str]         # the machine the input left, None when it left home
    why: str                    # "asked", "switch", a refusal's `why`, "refused", "no_answer", "link_lost"
    edge: Optional[str] = None  # where to land the pointer, when the move says
    offset: Optional[float] = None


@dataclass(frozen=True)
class Unreachable:
    peer: str
    why: str                    # a refusal's `why`, "refused", "no_answer", "link_lost", "unreachable"


@dataclass(frozen=True)
class Drop:
    peer: str


# What the dropped link says of itself until it reconnects.
DROPPED = "A hand-over to this machine was called off before it answered"


@dataclass(frozen=True)
class Local:
    pass


LOCAL = Local()


@dataclass
class _Wait:
    """A take this machine is waiting on an answer for."""
    peer: str
    route: int
    due: float
    chain: bool                 # a hand-over from the machine the input is on, holding the input
    reach: list
    edge: Optional[str] = None
    offset: Optional[float] = None


def _ident(message):
    """What a press is known by, so its release can be matched: the key's name or the button."""
    kind, data = message.get("type"), message.get("data") or {}
    if kind in ("keydown", "keyup"):
        return ("key", data.get("key"))
    if kind in ("mousedown", "mouseup"):
        return ("button", data.get("button"))
    return None


def _release(message):
    return {"type": _DOWNS[message["type"]], "data": dict(message["data"])}


class Owner:
    def __init__(self, own_id, clock, resistance_px=120):
        self.own_id = own_id
        self.resistance_px = resistance_px  # None for a phone, which has no hand at an edge
        self.route = 0
        self.on = None                      # the peer this machine's input is on, None at home
        self.reach_sent = []                # the `reach` last sent there
        self._clock = clock
        self._visit_route = 0               # the route of the take that began the current visit
        self._links = set()
        self._accepting = set()
        self._wait = None
        self._held = []
        self._left_out = {}                 # peer: until when
        self._takes = {}                    # route: (peer, when sent)
        self._pressed = {}                  # peer: {ident: the release}, in the order pressed
        self._swallow = set()               # presses whose physical release goes nowhere
        self._here = set()                  # presses let through here, whose release is let through too
        self._let_go = {}                   # peer: when it was let go, for its one clipboard
        self._clipboard_at = set()          # peers known to hold this machine's clipboard as it is

    @property
    def away(self):
        return self.on is not None

    def deadline(self):
        times = list(self._left_out.values())
        if self._wait is not None:
            times.append(self._wait.due)
        return min(times) if times else None

    def reach_for(self, target):
        now = self._clock()
        return sorted(peer for peer in self._links & self._accepting
                      if peer not in (target, self.own_id) and self._left_out.get(peer, now) <= now)

    # Links

    def link_up(self, peer, accepts):
        # A second link from a peer replaces the first, and ends its ownership there.
        actions = self.link_down(peer) if peer in self._links else []
        self._links.add(peer)
        self._set_accepting(peer, accepts)
        return actions + self._refresh_reach()

    def accepts(self, peer, accepts):
        if peer not in self._links:
            return []
        self._set_accepting(peer, accepts)
        return self._refresh_reach()

    def link_down(self, peer):
        self._links.discard(peer)
        self._accepting.discard(peer)
        # Its clipboard may still have been on the way, or waiting behind a slow read: sent again.
        self._clipboard_at.discard(peer)
        if peer == self.on:
            return self._come_home("link_lost")
        if self._wait is not None and self._wait.chain and self._wait.peer == peer:
            return self._not_taken(peer, "link_lost")
        self._forget_pressed(peer)
        return self._refresh_reach()

    def _set_accepting(self, peer, accepts):
        if accepts:
            self._accepting.add(peer)
        else:
            self._accepting.discard(peer)

    # Moves

    def go(self, peer, edge=None, offset=None):
        if peer is None or peer == self.own_id:
            return self._come_home("asked") if self.on is not None else []
        if peer == self.on:
            if self._wait is not None and self._wait.chain:
                # The machine being left disarmed its zones for the hand-over: re-arm it.
                return self._abandon() + self._stay() + self._replay_held()
            return []
        if peer not in self._links & self._accepting:
            return [Unreachable(peer, "unreachable")]
        if self.held_back(peer):
            return []
        left = self.on
        # A hand-over waiting on `peer` itself is not given up: this take follows it on that link.
        actions = self._abandon(unless=peer)
        self.route += 1
        if left is not None:
            actions += self._let_go_of(left, peer)
        actions += self._take(peer, edge, offset, chain=False)
        self._arrive(peer, self._wait.reach)
        actions += self._clipboard_to(peer)
        actions.append(Moved(peer, left, "asked", edge, offset))
        return actions + self._replay_held()

    def switch(self, peer, data):
        if peer != self.on:
            return []
        route, nxt = data.get("route"), data.get("next")
        edge, offset = data.get("edge"), data.get("offset")
        if nxt == self.own_id:
            if not isinstance(route, int) or route < self._visit_route:
                return []
            return self._come_home("switch", edge, offset)
        if route != self.route or (self._wait is not None and self._wait.chain):
            return []
        if nxt not in self.reach_sent or nxt not in self._links & self._accepting:
            return self._not_taken(nxt, "unreachable")
        self.route += 1
        return self._take(nxt, edge, offset, chain=True)

    def accept(self, peer, data):
        route = data.get("route")
        took = self._takes.get(route)
        # Not from a link this machine dropped: closing it already ended what this accept began.
        if took is None or took[0] != peer or peer not in self._links:
            return []
        wait = self._wait
        if wait is not None and wait.peer == peer and wait.route == route:
            self._wait = None
            if not wait.chain:
                return []
            left = self.on
            actions = self._let_go_of(left, peer)
            self._arrive(peer, wait.reach)
            actions += self._clipboard_to(peer)
            actions.append(Moved(peer, left, "switch", wait.edge, wait.offset))
            return actions + self._replay_held() + self._refresh_reach()
        if peer == self.on:
            return []
        # It took this machine's input after the wait for it ended: let it go again.
        return [Send(peer, {"type": "focus", "data": {"route": self.route, "target": self.on or self.own_id}})]

    def refuse(self, peer, data):
        route = data.get("route")
        why = data.get("why") if data.get("why") in REFUSALS else "refused"
        wait = self._wait
        if wait is not None and wait.chain:
            if peer == wait.peer and route == wait.route:
                return self._not_taken(peer, why)
            return []
        # The take's own route too: a re-arm sent before its answer came raised the route past it.
        if peer == self.on and (route == self.route or (wait is not None and route == wait.route)):
            self._hold_back(peer, why)
            return self._come_home(why)
        return []

    def tick(self):
        now = self._clock()
        actions = []
        wait = self._wait
        if wait is not None and now >= wait.due:
            if wait.chain:
                actions += self._abandon() + self._not_taken(wait.peer, "no_answer")
            else:
                actions += self._come_home("no_answer")
        expired = [peer for peer, until in self._left_out.items() if until <= now]
        for peer in expired:
            del self._left_out[peer]
        if expired:
            actions += self._refresh_reach()
        return actions

    # Input

    def input(self, message):
        kind, ident = message.get("type"), _ident(message)
        if ident in self._here:
            # Pressed while the input was here, so released here, never held for a hand-over:
            # a held event cannot be let through later.
            if kind in _UPS:
                self._here.discard(ident)
                return [LOCAL]
            return [LOCAL] if self.on is None else []
        if self._wait is not None and self._wait.chain:
            self._held.append(message)
            return []
        return self._route(message, replay=False)

    def _route(self, message, replay):
        kind, ident = message.get("type"), _ident(message)
        if ident in self._swallow:
            if kind in _UPS:
                self._swallow.discard(ident)
            return []
        if self.on is None:
            if not replay:
                if kind in _DOWNS:
                    self._here.add(ident)
                return [LOCAL]
            # Held for a hand-over that ended at home: the event cannot be given back, so a press
            # goes nowhere and so does its release.
            if kind in _DOWNS:
                self._swallow.add(ident)
            return []
        pressed = self._pressed.setdefault(self.on, {})
        if kind in _DOWNS:
            pressed[ident] = _release(message)
        elif kind in _UPS:
            pressed.pop(ident, None)
        return [Send(self.on, message)]

    def _replay_held(self):
        held, self._held = self._held, []
        return [action for message in held for action in self._route(message, replay=True)]

    # Clipboard

    def clipboard_changed(self):
        self._clipboard_at = set()

    def clipboard_arrived(self, peer, began_at, message):
        let_go = self._let_go.pop(peer, None)
        if let_go is None or began_at - let_go > CLIPBOARD_AFTER:
            return []
        self._clipboard_at = {peer}
        actions = [SetClipboard(message)]
        if self.on is not None:
            self._clipboard_at.add(self.on)
            actions.append(Send(self.on, message))
        return actions

    def expects_clipboard(self, peer, began):
        """Whether a frame from `peer` that began at `began` could be the one clipboard of its let-go,
        by `clipboard_arrived`'s own rule, however long it then took to arrive. A link asks this,
        without the app's lock, before it reads a frame too large for any other message."""
        let_go = self._let_go.get(peer)
        return let_go is not None and began - let_go <= CLIPBOARD_AFTER

    def _clipboard_to(self, peer):
        if peer in self._clipboard_at:
            return []
        self._clipboard_at.add(peer)
        return [SendClipboard(peer)]

    # The pieces

    def _take(self, peer, edge, offset, chain):
        reach = self.reach_for(peer)
        data = {"route": self.route, "target": peer}
        if edge is not None:
            data["edge"], data["offset"] = edge, offset
        if self.resistance_px is not None:
            data["resistance_px"] = self.resistance_px
        data["reach"] = reach
        now = self._clock()
        for route in [route for route, (_, when) in self._takes.items() if now - when > TAKE_MEMORY]:
            del self._takes[route]
        self._takes[self.route] = (peer, now)
        self._wait = _Wait(peer, self.route, now + TAKE_WAIT, chain, reach, edge, offset)
        return [Send(peer, {"type": "focus", "data": data})]

    def _arrive(self, peer, reach):
        self.on = peer
        self.reach_sent = reach
        self._visit_route = self.route

    def _let_go_of(self, peer, target):
        """The owner's releases of everything it pressed there, then the let-go `focus`."""
        pressed = self._pressed.pop(peer, {})
        self._swallow.update(pressed)
        self._let_go[peer] = self._clock()
        return [Send(peer, release) for release in pressed.values()] + [
            Send(peer, {"type": "focus", "data": {"route": self.route, "target": target}})]

    def _forget_pressed(self, peer):
        self._swallow.update(self._pressed.pop(peer, {}))

    def _come_home(self, why, edge=None, offset=None):
        left = self.on
        if self._wait is not None and not self._wait.chain:
            # Never taken: a clipboard sent after the take was dropped there.
            self._clipboard_at.discard(left)
        actions = self._abandon()
        self.route += 1
        if left in self._links:
            actions += self._let_go_of(left, self.own_id)
        else:
            self._forget_pressed(left)
        self.on = None
        self.reach_sent = []
        actions.append(Moved(None, left, why, edge, offset))
        return actions + self._replay_held()

    def _abandon(self, unless=None):
        """Ends the wait. A hand-over given up before its answer closes the next machine's link,
        first, so a late `accept` cannot leave that machine owned while nobody drives it: the
        responder ends an ownership when its link ends (section 5). That machine is out of `reach`
        until its link is up again."""
        wait, self._wait = self._wait, None
        if wait is None or not wait.chain or wait.peer == unless:
            return []
        self._links.discard(wait.peer)
        self._accepting.discard(wait.peer)
        self._clipboard_at.discard(wait.peer)
        return [Drop(wait.peer)]

    def _not_taken(self, peer, why):
        """A hand-over from the machine the input is on did not happen: re-arm it without `peer`."""
        self._wait = None
        if peer is not None:
            self._hold_back(peer, why)
        return self._stay() + [Unreachable(peer, why)] + self._replay_held()

    def held_back(self, peer):
        """Whether `peer` refused, or did not take, a move a moment ago and is not asked again yet."""
        return self._left_out.get(peer, 0) > self._clock()

    def _hold_back(self, peer, why):
        """`peer` is not asked again for as long as it would refuse: each push at its edge would
        otherwise ask, be refused, and say so."""
        self._left_out[peer] = self._clock() + (SENT_HOME_FOR if why == "sent_home" else LEFT_OUT)

    def _stay(self):
        self.route += 1
        self.reach_sent = self.reach_for(self.on)
        data = {"route": self.route, "target": self.on, "stay": True}
        if self.resistance_px is not None:
            data["resistance_px"] = self.resistance_px
        data["reach"] = self.reach_sent
        return [Send(self.on, {"type": "focus", "data": data})]

    def _refresh_reach(self):
        if self.on is None or (self._wait is not None and self._wait.chain):
            return []
        if self.reach_for(self.on) == self.reach_sent:
            return []
        return self._stay()
