"""Administrator-pinned authority and the input-only secure desktop v6 listener.

This module has no Windows imports. Protected storage, reading the fixed source
settings file and proving the active console's Winlogon desktop belong to the
worker. A source settings file can revoke a protected grant, never create one.
"""

import copy
import hmac
import os

from . import protocol, receiver


SECURE_PORT = 24822
MAX_FRAME_BYTES = 4096
DESKTOP_PLATFORMS = frozenset({"windows", "macos", "linux"})
_PAYLOAD_FIELDS = {"machine_id", "name", "source_settings", "peers"}
_GRANT_FIELDS = {"id", "token", "platform", "in_use", "allow_drive"}
_INPUT_TYPES = frozenset({
    protocol.MSG_KEYDOWN, protocol.MSG_KEYUP, protocol.MSG_MOUSEMOVE,
    protocol.MSG_MOUSEDOWN, protocol.MSG_MOUSEUP, protocol.MSG_SCROLL,
})
_MESSAGE_TYPES = _INPUT_TYPES | {protocol.MSG_FOCUS, protocol.MSG_PING}


class GrantPolicy:
    """Validate a decrypted administrator snapshot and intersect it with settings.

    ``payload`` has exactly ``machine_id`` (the canonical v6 ID), ``name``,
    ``source_settings`` (an absolute file path), and ``peers``. Each peer has
    exactly ID, paired token, desktop platform, ``in_use=True`` and
    ``allow_drive=True``. Invalid snapshots raise ValueError before listening.

    ``source_load()`` reads the source_settings path, chosen by the protected
    snapshot rather than a peer. It returns the ordinary settings dictionary or
    raises; missing, unreadable or malformed settings confer no authority.
    Callers must bound that read and reject reparse points. Neither this class
    nor the listener ever writes source settings or the protected snapshot.
    """

    def __init__(self, payload, source_load):
        if not callable(source_load):
            raise ValueError("a source settings reader is required")
        if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS:
            raise ValueError("invalid secure input snapshot")
        machine_id = protocol.read_id(payload.get("machine_id"))
        path = payload.get("source_settings")
        peers = payload.get("peers")
        if (machine_id is None or not protocol.valid_name(payload.get("name"))
                or not isinstance(path, str) or not os.path.isabs(path) or "\0" in path
                or not isinstance(peers, list) or not 1 <= len(peers) <= protocol.MAX_PEERS):
            raise ValueError("invalid secure input snapshot")
        ids, tokens = set(), set()
        for peer in peers:
            if (not isinstance(peer, dict) or set(peer) != _GRANT_FIELDS
                    or protocol.read_id(peer.get("id")) is None
                    or peer["id"] == payload["machine_id"]
                    or not protocol.is_paired_token(peer.get("token"))
                    or not isinstance(peer.get("platform"), str)
                    or peer.get("platform") not in DESKTOP_PLATFORMS
                    or peer.get("in_use") is not True or peer.get("allow_drive") is not True
                    or peer["id"] in ids or peer["token"] in tokens):
                raise ValueError("invalid secure input grant")
            ids.add(peer["id"])
            tokens.add(peer["token"])
        self._grants = copy.deepcopy(peers)
        self._machine_id = machine_id
        self._machine_text = payload["machine_id"]
        self._name = payload["name"]
        self._source_settings = path
        self._source_load = source_load

    @property
    def machine_id(self):
        return self._machine_id

    @property
    def source_settings(self):
        return self._source_settings

    def identity(self):
        return {"id": self._machine_id, "name": self._name, "platform": "windows",
                "app": "1.6.0", "caps": []}

    def peers(self):
        """Fresh copies of effective grants; source changes can only remove them."""
        try:
            current = self._source_load()
            if (not isinstance(current, dict) or current.get("machine_id") != self._machine_text
                    or not isinstance(current.get("peers"), list)):
                return []
            by_id, tokens = {}, set()
            for peer in current["peers"]:
                if not isinstance(peer, dict):
                    return []
                ident, token = peer.get("id"), peer.get("token")
                if not isinstance(ident, str) or not isinstance(token, str) or ident in by_id or token in tokens:
                    return []
                by_id[ident] = peer
                tokens.add(token)
            allowed = []
            for grant in self._grants:
                peer = by_id.get(grant["id"])
                if (peer is not None and peer.get("in_use", True) is True
                        and peer.get("allow_drive") is True and protocol.linkable(peer)
                        and peer.get("platform") == grant["platform"]
                        and hmac.compare_digest(peer["token"], grant["token"])):
                    allowed.append(copy.deepcopy(grant))
            return allowed
        except Exception:
            return []

    def permits(self, peer_id, key_id, platform):
        for peer in self.peers():
            if (peer["id"] == protocol.id_text(peer_id) and peer["platform"] == platform
                    and hmac.compare_digest(protocol.key_id(peer["token"]), key_id)):
                return True
        return False


class _GrantBook(receiver.PeerBook):
    def __init__(self, policy):
        self._policy = policy
        super().__init__(lambda: {"peers": policy.peers(), "zones": []}, lambda _settings: None)

    def peers(self):
        return self._policy.peers()

    read = peers

    def zones(self):
        return []

    def admit(self, key_id, own_id, peer_id, changes):
        entry = self.find(key_id)
        if entry is None:
            return receiver.GONE, None
        if own_id != self._policy.machine_id or peer_id == own_id or entry["id"] != protocol.id_text(peer_id):
            return receiver.WRONG_ID, None
        if changes.get("platform") != entry["platform"]:
            return receiver.IN_USE_OFF, None
        return receiver.ADMITTED, entry

    def store_paired(self, peer, ids):
        return None

    def mark_dormant(self, token, reason):
        return False


class _NoClipboard:
    @staticmethod
    def change_stamp():
        return None


class _SecureReceiver(receiver.LinkResponder):
    def __init__(self, policy, *, injector, desktop, available, status_callback):
        self._policy = policy
        self._desktop_available = available
        super().__init__(
            _GrantBook(policy), policy.identity, status_callback=status_callback,
            injector=injector, desktop=desktop, clipboard=_NoClipboard(),
            available=self._ready, max_frame_bytes=MAX_FRAME_BYTES,
        )

    def _ready(self):
        try:
            return self._desktop_available() is True and bool(self._policy.peers())
        except Exception:
            return False

    def _check_link(self, link):
        if (not link.closed.is_set() and self._ready()
                and self._policy.permits(link.peer_id, link.key_id, link.peer_platform)):
            return True
        with self._lock:
            self._end_locked(link)
            link.close()
        return False

    def _dispatch(self, link, message):
        kind = message.get("type") if isinstance(message, dict) else None
        if kind not in _MESSAGE_TYPES:
            with self._lock:
                after = self._end_locked(link)
                link.close()
            self._run(after)
            return
        if self._check_link(link):
            super()._dispatch(link, message)

    def _inject_locked(self, link, read, *, scaled=False):
        if read["type"] in _INPUT_TYPES and self._check_link(link):
            try:
                super()._inject_locked(link, read, scaled=scaled)
            except Exception:
                self._end_locked(link)
                link.close()
                self._set_status(receiver.ServerState.ERROR, "Secure input injection failed")

    def _release_locked(self, link):
        # The ordinary receiver's exception logs include key names and backend
        # exception text, either of which can contain privileged typed input.
        failed = False
        for (kind, name), us in reversed(list(link.held.items())):
            try:
                if kind == "key":
                    self._injector.inject_key(name, down=False, us=us)
                else:
                    self._injector.inject_mouse_button(name, down=False)
            except Exception:
                failed = True
        link.held.clear()
        try:
            release_all = getattr(self._injector, "release_all", None)
        except Exception:
            release_all = None
            failed = True
        try:
            if release_all is not None:
                release_all()
        except Exception:
            failed = True
        if failed:
            self._set_status(receiver.ServerState.ERROR, "Secure input release failed")

    def _refusal_locked(self, link, focus):
        if not self._check_link(link):
            return "not_allowed", None
        return super()._refusal_locked(link, focus)

    def _home_if_locked(self, link):
        # The writer's idle heartbeat also revokes peers that send nothing.
        self._check_link(link)

    def _land(self, link, focus, visit):
        # Focus grants ownership only. It never changes a desktop or clears a lock.
        with self._lock:
            if self._arriving_visit == visit:
                self._arriving_visit = None

    def _clipboard_back(self, link, mark_or_visit, visit=None):
        return None


def create_receiver(policy, *, injector, desktop, available, status_callback):
    """Create an input-only LinkResponder; the caller owns start(port) and stop().

    ``available()`` must return the boolean True only for the worker's own active
    console session on Winlogon. Exceptions, other values or revoked settings end
    ownership and links, releasing held input. ``injector`` must bind each input
    thread to that worker desktop before injection and release. These dependencies
    are required: the privileged listener never falls back to user-app modules.
    """
    if (not isinstance(policy, GrantPolicy) or injector is None or desktop is None
            or not callable(available) or not callable(status_callback)):
        raise ValueError("secure input requires policy, injector, desktop and state callbacks")
    return _SecureReceiver(policy, injector=injector, desktop=desktop, available=available,
                           status_callback=status_callback)
