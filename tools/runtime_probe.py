"""build_dmg.sh runs this with the bundle's own python, which is signed with the hardened runtime, to
prove the app's code loads and its callbacks work without entitlements. No window, no permission
prompt, and nothing from the source tree on sys.path."""
import base64
import os
import sys

import objc, AppKit, Foundation, Quartz, ApplicationServices, CoreFoundation as CF, rumps

import bridge, clipboard_mac, config, crossing, gestures, key_codes, link_state, media_keys
import notch_beam, notch_island, pages, pointer_hide, previews, settings_store, theme, tokens
from core import pairing, protocol
import wake, widgets

assert ".app/" in bridge.__file__, bridge.__file__

# The link's X25519 and ChaCha20-Poly1305 and pairing's X25519 are all libsodium, reached through
# PyNaCl's own cffi extension: a link's handshake, a sealed frame and a whole pairing exchange prove
# that extension is in the bundle and loads under the hardened runtime.
probe_token = base64.urlsafe_b64encode(os.urandom(protocol.TOKEN_SIZE)).decode("ascii").rstrip("=")
one = protocol.LinkSession(probe_token, protocol.ROLE_INITIATOR)
two = protocol.LinkSession(probe_token, protocol.ROLE_RESPONDER)
two.accept_preamble(one.preamble())
one.accept_preamble(two.preamble())
assert two.open(one.seal({"type": "ping", "data": {}})[protocol.HEADER_SIZE:]) == {"type": "ping", "data": {}}

pairing_host = pairing.PairingHost()
pairing_code = pairing_host.begin()
pairing_client = pairing.PairingClient(pairing_host.pair_id, pairing_code, "probe")
pairing_confirm = pairing_client.accept(pairing_host.handle(pairing_client.start()))
assert pairing_client.finish(pairing_host.handle(pairing_confirm)) == pairing_host.paired[0]


class Probe(Foundation.NSObject):
    def ping_(self, value):
        self.value = value


# A Python method called from Objective-C, and a C callback: both run through libffi closures, the
# thing the hardened runtime's executable-memory rules would break.
probe = Probe.new()
probe.performSelector_withObject_(b"ping:", 7)
assert probe.value == 7

fired = []
timer = CF.CFRunLoopTimerCreate(None, CF.CFAbsoluteTimeGetCurrent(), 0, 0, 0, lambda t, i: fired.append(1), None)
CF.CFRunLoopAddTimer(CF.CFRunLoopGetCurrent(), timer, CF.kCFRunLoopDefaultMode)
CF.CFRunLoopRunInMode(CF.kCFRunLoopDefaultMode, 0.3, False)
assert fired

print(f"Hardened runtime probe passed under {sys.executable}")
