"""Qualify the bundled Mac controller, native UI refresh and encrypted loopback link."""
import argparse
import base64
import copy
import logging
from pathlib import Path
import platform
import socket
import sys
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument('--seconds', type=float, default=600)
parser.add_argument('--expected-arch', choices=('arm64', 'x86_64'), required=True)
args = parser.parse_args()
print('runtime', sys.version, platform.machine(), flush=True)
assert platform.machine() == args.expected_arch, platform.machine()
import bridge
import config
from wake import WakingController
from core import protocol, receiver
print('bundled controller', bridge.__file__, flush=True)
assert '.app/' in bridge.__file__, bridge.__file__

token = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')
own = bytes(range(16))
peer = bytes(range(32, 48))
data = {'schema': 6, 'machine_id': protocol.id_text(own), 'peers': [{
    'id': protocol.id_text(peer), 'name': 'Synthetic peer', 'platform': 'windows',
    'token': token, 'host': '127.0.0.1', 'port': 0, 'send': True, 'allow_drive': False,
    'linked': True, 'paired_at': 1790000000, 'paired_with': [], 'from_1_4': False,
}]}
book = receiver.PeerBook(lambda: copy.deepcopy(data), lambda value: data.update(value))
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
data['peers'][0]['port'] = listener.getsockname()[1]
failures = []
received = []
stop = threading.Event()
def serve():
    try:
        connection, _ = listener.accept()
        with connection:
            session = protocol.LinkSession(token, protocol.ROLE_RESPONDER)
            session.accept_preamble(protocol.recv_link_preamble(connection, time.monotonic() + 5))
            connection.sendall(session.preamble())
            hello = protocol.read_hello(protocol.recv_first_msg(connection, session, time.monotonic() + 5))
            print('received hello', hello['name'], flush=True)
            protocol.send_msg(connection, session, protocol.welcome_v6(peer, 'Synthetic peer', 'windows', '1.5.20', ['ping_rtt'], False))
            connection.settimeout(3)
            while not stop.is_set():
                message = protocol.recv_msg(connection, session)
                received.append(message['type'])
                if message['type'] == protocol.MSG_PING:
                    fields = message.get('data', {})
                    protocol.send_msg(connection, session, protocol.ping_msg(echo=fields['probe']) if 'probe' in fields else protocol.ack_v6(0, 0))
    except Exception as exc:
        if not stop.is_set():
            failures.append(repr(exc))
threading.Thread(target=serve, daemon=True).start()
cfg = config.Config(host='127.0.0.1', port=listener.getsockname()[1], auth_token=token,
                    check_updates=False, hide_idle_pointer=False, allow_windows_to_drive=False)
logger = logging.getLogger('MAC1521')
logger.addHandler(logging.StreamHandler(sys.stdout))
logger.setLevel(logging.INFO)
# The controller starts its real network workers. Input capture is a separate method and is never called.
class NoInputQuartz:
    def __getattr__(self, name):
        if name in {'CGAssociateMouseAndMouseCursorPosition', 'CGDisplayShowCursor', 'CGDisplayHideCursor'}:
            return lambda *args: 0
        if name.startswith(('CGEventTapCreate', 'CGEventPost', 'CGWarpMouse')):
            raise AssertionError('Input operation forbidden in probe: ' + name)
        return getattr(bridge.Quartz, name)

controller = WakingController(cfg, quartz=NoInputQuartz(), book=book, identity=lambda: {
    'id': own, 'name': 'Synthetic Mac', 'platform': 'macos', 'app': '1.5.20',
    'caps': ['ping_rtt'], 'port': 0}, logger=logger)
import json
from types import SimpleNamespace
from unittest import mock
sys.path.insert(0, str(Path(sys.prefix)))
import kvm_bridge_app as app
import settings_store
import rumps.rumps as rumps_module
from Foundation import NSDate, NSRunLoop
app.AppKit.NSApplication.sharedApplication()
app.AppKit.NSApp.setActivationPolicy_(app.AppKit.NSApplicationActivationPolicyProhibited)
Path.home().mkdir(parents=True, exist_ok=True)
path = Path.home() / 'config.json'
path.write_text(json.dumps(settings_store.config_to_raw(cfg)))
store = settings_store.SettingsStore(path)
settings = store.current()
settings.update(machine_id=data['machine_id'], peers=[{**settings_store.PEER_DEFAULTS, **data['peers'][0]}])
store.save_settings(settings)
controller.start_input_capture = lambda: False
with mock.patch.object(app, 'accessibility_granted', return_value=False), mock.patch.object(app, 'input_monitoring_granted', return_value=False):
    window = app.ControlWindow.alloc().initWithController_settingsStore_logger_(controller, store, logger)
window.windows_input = mock.Mock()
window.windows_input.server.links.return_value = []
window.windows_input.server.owner = None
window.windows_input.server.caps_of.return_value = None
tray = SimpleNamespace(controller=controller, menu=rumps_module.Menu(), _send_keys=[],
                       toggle_item=app.rumps.MenuItem(app.TOGGLE_ITEM), _send_to=lambda peer: None)
tray.menu.add(tray.toggle_item)
tray._peer_live = lambda entry: app.TrayApp._peer_live(tray, entry)
print('offscreen window and native menu ready', flush=True)
controller.start()
deadline = time.monotonic() + 10
while not controller.connected and time.monotonic() < deadline and not failures:
    time.sleep(0.05)
assert controller.connected, (controller.connection_status, failures)
print('CONNECTED', flush=True)
end = time.monotonic() + args.seconds
try:
    while time.monotonic() < end:
        assert controller.connected, controller.connection_status
        assert not failures, failures
        window.refresh()
        app.TrayApp._machine_items(tray)
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
        time.sleep(0.1)
finally:
    stop.set()
    controller.stop()
    window.appearance_watch.stop()
    listener.close()
assert len(received) >= args.seconds / 3, received
print('PASS', platform.machine(), args.seconds, 'seconds connected;', len(received), 'encrypted frames', flush=True)
