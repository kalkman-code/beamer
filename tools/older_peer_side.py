"""The 1.4.x half of tools/older_peer_check.py, run with a 1.4.x source tree's own code and never
this tree's: that tree's ReceiverServer (the PC's) listens on one port, and its two initiators, the
Mac's KVMController and the PC's MacSender, dial this tree's responder, each built the way that
tree's mac_app/tests/two_machines.py builds them, with only the platform edges faked.

    python older_peer_side.py <1.4.x tree> <port to listen on> <version 6 responder's port>

The token comes on stdin, never the command line. Prints one JSON object per line: what each part
showed, then "done"."""

import json
import os
import sys
import time

OLD, LISTEN, DIAL = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
TOKEN = sys.stdin.readline().strip()
for path in (os.path.join(OLD, "mac_app"), os.path.join(OLD, "mac_app", "tests")):
    sys.path.insert(0, path)
for path in (os.path.join(OLD, "win_app"), os.path.join(OLD, "win_app", "tests")):
    sys.path.append(path)

import app_config  # noqa: E402
import receiver  # noqa: E402
import sender as pc_sender_module  # noqa: E402
from bridge import KVMController  # noqa: E402
from fakes import FakeClipboard, FakeDesktop, FakeInjector  # noqa: E402
from return_edge import Rect  # noqa: E402
from test_bridge import FakeClock, FakeQuartz, crossing_config, quiet_logger  # noqa: E402
from two_machines import NoUnlock  # noqa: E402


def say(**fields):
    print(json.dumps(fields), flush=True)


def settle(read, seconds=3.0):
    """What `read()` shows once it has stopped changing, or after `seconds`."""
    deadline = time.monotonic() + seconds
    last, since = read(), time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.05)
        now = read()
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since > 1.0:
            break
    return last


statuses = []
server = receiver.ReceiverServer(
    status_callback=lambda state, detail: statuses.append(f"{state.value}: {detail}"),
    clipboard=FakeClipboard(),
    unlock=NoUnlock(),
    desktop=FakeDesktop([Rect(0, 0, 1920, 1080)], cursor=(900, 500)),
    injector=FakeInjector(),
    focus_callback=lambda target: None,
)
server.start(app_config.Config(host="127.0.0.1", port=LISTEN, auth_token=TOKEN))
say(part="ready")
sys.stdin.readline()

cfg = crossing_config()
cfg.host, cfg.port, cfg.auth_token = "127.0.0.1", DIAL, TOKEN
cfg.reconnect_interval_s = 0.2
mac = KVMController(cfg, logger=quiet_logger(), quartz=FakeQuartz, clock=FakeClock(), desktop_bounds=lambda: (0, 0, 1728, 1117))
mac.on_user_alert = lambda title, message: say(part="1.4.3 Mac alert", text=f"{title}: {message}")
mac.start()
time.sleep(0.5)
say(part="1.4.3 Mac, dialling version 6", text=settle(lambda: mac.connection_status), connected=bool(mac.connected))
mac.stop()

pc = pc_sender_module.MacSender(desktop=FakeDesktop([Rect(0, 0, 1920, 1080)]), clipboard=FakeClipboard(), is_local=lambda host: False)
pc.start(app_config.Config(host="127.0.0.1", port=DIAL, auth_token=TOKEN, mac_host="127.0.0.1"))
time.sleep(0.5)
say(part="1.4.3 PC, dialling version 6", text=settle(lambda: pc.status), connected=bool(pc.connected))
pc.stop()

say(part="1.4.3 PC's listener, dialled by version 6", text=" | ".join(dict.fromkeys(statuses)))
server.stop()
say(part="done")
