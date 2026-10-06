"""Beamer 1.4.x and this build refuse each other, both ways, in words each side shows (WIRE.md
section 2, Versions; docs/wire-v6-tests.md, "Older peers": the live check). Everything runs on
loopback on this machine, on spare ports; nothing is installed, and no Beamer config is read or
written.

The 1.4.x side is a 1.4.x source tree run with its own code (tools/older_peer_side.py): its
responder, and its Mac's and PC's initiators. This side is core.receiver.LinkResponder and
core.link.OutboundLink, each holding the peer as a 1.4.x pairing carried into 1.5.0 would be held
(no id yet, `from_1_4`). The token is generated per run and goes to the other process on stdin.

    git archive acead88 | tar -x -C <dir>          # 1.4.3's source
    python3 tools/older_peer_check.py <dir>

Exit status 0 when every side refused and said so."""

import base64
import json
import os
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from core import receiver  # noqa: E402
from core.link import OutboundLink  # noqa: E402
from core.tests.responder_harness import Machine, Settings, entry, ident, wait_for  # noqa: E402


def free_port():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def migrated(name, token, port=24820):
    """A 1.4.x pairing as 1.5.0 carries it over: no id until the peer says it."""
    return entry("", name, token=token, host="127.0.0.1", port=port, linked=False, from_1_4=True, platform="")


def main(old_tree):
    token = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    here = Machine([migrated("Old Beamer", token)], own=ident(7), name="New").start()
    old_port = free_port()
    side = subprocess.Popen(
        [sys.executable, os.path.join(_ROOT, "tools", "older_peer_side.py"), old_tree, str(old_port), str(here.port)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    side.stdin.write(token + "\n")
    side.stdin.flush()
    seen = {}
    assert json.loads(side.stdout.readline())["part"] == "ready", "the 1.4.x side did not start"

    # This build dials the 1.4.x listener.
    settings = Settings([migrated("Old PC", token, old_port)], own=ident(9))
    book = receiver.PeerBook(settings.load, settings.save)
    states = []
    link = OutboundLink(
        token, book,
        lambda: {"id": ident(9), "name": "New", "platform": "macos", "app": "1.5.0", "caps": [], "port": 24820},
        state=lambda link_, up, text: states.append((up, text)), reconnect_seconds=0.5,
    )
    link.start()
    wait_for(lambda: states, timeout=5)
    time.sleep(1.5)
    link.stop()
    seen["1.5.0 initiator, dialling 1.4.x"] = " | ".join(dict.fromkeys(text for _, text in states))

    side.stdin.write("go\n")
    side.stdin.flush()
    for line in side.stdout:
        part = json.loads(line)
        if part["part"] == "done":
            break
        seen[part["part"]] = part.get("text", "") + ("" if part.get("connected") is not True else "  [CONNECTED]")
    side.wait(timeout=10)
    seen["1.5.0 responder, dialled by 1.4.x"] = " | ".join(dict.fromkeys(f"{state.value}: {detail}" for state, detail in here.statuses))
    here.stop()

    for part, text in seen.items():
        print(f"{part}:\n    {text}")
    refused = (
        "Update Beamer on Old PC" in seen["1.5.0 initiator, dialling 1.4.x"]
        and "Old Beamer" in seen["1.5.0 responder, dialled by 1.4.x"]
        and not any("[CONNECTED]" in text for text in seen.values())
        and all("v6" in seen.get(part, "") for part in ("1.4.3 Mac, dialling version 6", "1.4.3 PC, dialling version 6", "1.4.3 PC's listener, dialled by version 6"))
    )
    print("refused both ways, in words" if refused else "NOT AS EXPECTED")
    return 0 if refused else 1


if __name__ == "__main__":
    sys.exit(main(os.path.abspath(sys.argv[1])))
