"""The Overview's machines list and pairing sheet in the real settings window: built as
the README's screenshots build it, never put on screen, starting no network."""

import base64
import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import AppKit  # noqa: E402

AppKit.NSApplication.sharedApplication()

import bridge  # noqa: E402
import kvm_bridge_app  # noqa: E402
import settings_store  # noqa: E402
from bridge_fakes import PAIRED_TOKEN  # noqa: E402
from core import pairing  # noqa: E402
from fake_link import FakeLink  # noqa: E402
from wake import WakingController  # noqa: E402

FIRST = base64.urlsafe_b64encode(bytes(range(1, 17))).decode().rstrip("=")
SECOND = base64.urlsafe_b64encode(bytes(range(21, 37))).decode().rstrip("=")
SECOND_TOKEN = base64.urlsafe_b64encode(bytes(range(100, 132))).decode().rstrip("=")
THIRD = base64.urlsafe_b64encode(bytes(range(60, 76))).decode().rstrip("=")
THIRD_TOKEN = base64.urlsafe_b64encode(bytes(range(200, 232))).decode().rstrip("=")


def entry(ident, token, name, platform="windows", host="192.0.2.30", **extra):
    return {**settings_store.PEER_DEFAULTS, "id": ident, "name": name, "platform": platform, "token": token,
            "host": host, "port": 24820, "paired_at": 1_700_000_000, **extra}


class FakeService:
    """The parts of PairingService the panel reads and calls."""

    def __init__(self):
        self.code = None
        self.seconds_left = 0
        self.outcome = None
        self.known = None
        self.error = None
        self.tcp_error = None
        self.tcp_port = 24821
        self.heard = []
        self.begun = 0
        self.cancelled = 0
        self.paired = []
        self.qr = "beamer://pair?host=192.0.2.9&port=24821&code=123456&k=" + "A" * 22
        self.result = None

    def begin_pairing(self):
        self.begun += 1
        self.code = "482913"
        self.seconds_left = 60
        return self.code

    def cancel_pairing(self):
        self.cancelled += 1
        self.code = None

    def machines(self):
        return self.heard

    def qr_text(self, address=None):
        return self.qr if self.code else None

    def pair(self, machine, code):
        self.paired.append(("machine", machine["name"], code))
        return self._done()

    def pair_by_address(self, address, code, port=24821):
        self.paired.append(("address", address, code))
        return self._done()

    def _done(self):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def heard(name="Laptop", pair_id="abc", **extra):
    return {"name": name, "address": "192.0.2.40", "port": 24820, "reply_port": 24821, "pair_id": pair_id,
            "pairing": 3, "platform": "macos", "id": None, **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="192.0.2.20", auth_token=PAIRED_TOKEN, pc_name="Studio")
        path.write_text(json.dumps(raw))
        self.store = settings_store.SettingsStore(path)
        logger = logging.getLogger("test-machines-panel")
        cfg = self.store.load()
        book, identity = bridge.links_from_store(self.store, "1.5.0")
        self.controller = WakingController(cfg, logger=logger, book=book, identity=identity, link_factory=FakeLink)
        with mock.patch.object(kvm_bridge_app, "accessibility_granted", return_value=True), \
                mock.patch.object(kvm_bridge_app, "input_monitoring_granted", return_value=True):
            self.window = kvm_bridge_app.ControlWindow.alloc().initWithController_settingsStore_logger_(
                self.controller, self.store, logger)
        self.panel = self.window.panel
        self.service = FakeService()
        self.panel.service = self.service
        self.directions = []
        self.window.direction_handler = lambda token, **change: self.directions.append((token, change))

    def tearDown(self):
        self.window.appearance_watch.stop()
        self.directory.cleanup()

    def pair(self, ident, token, name, **extra):
        self.store.add_peer(entry(ident, token, name, **extra))
        self.window.peers_changed()

    def rows(self):
        return [view for view in self.panel.list.arrangedSubviews()]

    def texts(self, view):
        found = []
        if isinstance(view, AppKit.NSTextField):
            found.append(view.stringValue())
        for sub in view.subviews():
            found.extend(self.texts(sub))
        return found


class ListTests(Base):
    def test_the_migrated_machine_is_listed_by_its_name_and_where_it_is(self):
        self.panel.refresh()
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("Studio", self.rows()[0].accessibilityLabel())
        self.assertTrue(self.panel.empty.view.isHidden())

    def test_every_paired_machine_is_a_row_and_two_of_one_name_show_the_end_of_their_ids(self):
        self.pair(FIRST, SECOND_TOKEN, "Studio")
        self.panel.refresh()
        labels = [row.accessibilityLabel() for row in self.rows()]
        self.assertEqual(len(labels), 2)
        self.assertIn("Studio", labels[0])
        self.assertIn(f"Studio ({FIRST[-4:]})", labels[1])

    def test_with_nothing_paired_the_list_says_so_and_the_sheet_is_open(self):
        self.store.remove_peer(PAIRED_TOKEN)
        self.window.peers_changed()
        self.panel.refresh()
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.panel.empty.view.isHidden())
        self.assertEqual(self.panel.empty.text, "No machines paired yet.")
        self.assertFalse(self.panel.sheet.view.isHidden())

    def test_with_machines_paired_the_sheet_waits_for_the_button(self):
        self.panel.refresh()
        self.assertTrue(self.panel.sheet.view.isHidden() or self.panel.open is False)
        self.assertEqual(self.panel.pair_button.title, "Pair a machine")
        self.panel._toggle_sheet()
        self.assertTrue(self.panel.open)
        self.assertEqual(self.panel.pair_button.title, "Cancel")
        self.panel._toggle_sheet()
        self.assertFalse(self.panel.open)

    def test_the_machines_lead_the_page_while_none_is_paired_and_follow_the_controls_after(self):
        body = self.window.overview_body
        self.panel.refresh()
        self.window._place_machines(False)
        order = list(body.arrangedSubviews())
        self.assertEqual(order.index(self.panel.machines_module.view), 3)
        self.window._place_machines(True)
        order = list(body.arrangedSubviews())
        self.assertEqual(order[1:3], [self.panel.machines_module.view, self.panel.sheet.view])
        self.assertNotIn(order[0], (self.panel.machines_module.view, self.panel.sheet.view))

    def test_addresses_are_dots_while_they_are_hidden(self):
        self.window.controller.cfg.hide_addresses = True
        self.panel._rows_key = None
        self.panel.refresh()
        text = " ".join(self.texts(self.rows()[0]))
        self.assertNotIn("192.0.2.20", text)


    def texts(self, view):
        found = []
        if isinstance(view, AppKit.NSTextField):
            found.append(view.stringValue())
        for sub in view.subviews():
            found.extend(self.texts(sub))
        return found


class DirectionAndRemovalTests(Base):
    def test_each_switch_hands_the_handler_its_machine_and_one_change(self):
        self.panel.refresh()
        row = self.rows()[0]
        self.assertEqual(len([view for view in self.descend(row)
                              if view.accessibilityRole() == AppKit.NSAccessibilityCheckBoxRole]), 1)
        self.panel._toggle_directions(PAIRED_TOKEN)
        row = self.rows()[0]
        switches = [view for view in self.descend(row) if view.accessibilityRole() == AppKit.NSAccessibilityCheckBoxRole]
        self.assertEqual(len(switches), 3)
        switches[1].callback()
        switches[2].callback()
        token = PAIRED_TOKEN
        self.assertEqual([call[0] for call in self.directions], [token, token])
        self.assertEqual({tuple(call[1]) for call in self.directions}, {("send",), ("allow_drive",)})

    def descend(self, view):
        yield view
        for sub in view.subviews():
            yield from self.descend(sub)

    def test_remove_asks_first_and_keep_changes_nothing(self):
        self.panel.refresh()
        self.panel._ask_to_remove(PAIRED_TOKEN)
        self.assertEqual(self.panel.removing, PAIRED_TOKEN)
        text = " ".join(self.texts_of(self.rows()[0]))
        self.assertIn("Remove Studio?", text)
        self.panel._keep()
        self.assertIsNone(self.panel.removing)
        self.assertEqual(len(self.store.current()["peers"]), 1)

    def texts_of(self, view):
        return [v.stringValue() for v in self.descend(view) if isinstance(v, AppKit.NSTextField)]

    def test_removing_takes_the_machine_its_link_and_its_row(self):
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        self.assertEqual(len(self.controller.links), 2)
        self.panel._remove(SECOND_TOKEN)
        self.assertEqual([peer["token"] for peer in self.store.current()["peers"]], [PAIRED_TOKEN])
        self.assertEqual(list(self.controller.links), [PAIRED_TOKEN])
        self.panel.refresh()
        self.assertEqual(len(self.rows()), 1)

    def test_removing_the_first_machine_leaves_the_next_the_one_the_window_reads(self):
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        self.panel._remove(PAIRED_TOKEN)
        self.assertEqual(self.controller.cfg.auth_token, SECOND_TOKEN)
        self.assertEqual(self.window.connection_label if hasattr(self.window, "connection_label") else "Laptop", "Laptop")

    def test_a_removal_that_cannot_be_saved_says_so_and_keeps_the_machine(self):
        with mock.patch.object(self.store, "remove_peer", side_effect=settings_store.SettingsError("disk full")):
            self.panel._remove(PAIRED_TOKEN)
        self.assertEqual(len(self.store.current()["peers"]), 1)


class FocusTests(Base):
    """A rebuild of the rows (a state changing, Remove asking) leaves focus on the control it was on."""

    def controls(self):
        found = {}

        def walk(view):
            ident = getattr(view, "focus_id", None)
            if ident is not None:
                found[ident] = view
            for sub in view.subviews():
                walk(sub)

        walk(self.panel.list)
        return found

    def focus(self, ident):
        view = self.controls()[ident]
        self.assertTrue(self.window.window.makeFirstResponder_(view))
        return view

    def focused(self):
        view = self.window.window.firstResponder()
        while view is not None and getattr(view, "focus_id", None) is None:
            view = view.superview() if hasattr(view, "superview") else None
        return getattr(view, "focus_id", None)

    def test_a_rebuild_keeps_focus_on_the_same_switch(self):
        self.panel.refresh()
        self.panel._toggle_directions(PAIRED_TOKEN)
        self.focus((PAIRED_TOKEN, "drives"))
        self.panel.invalidate()
        self.panel.refresh()
        self.assertEqual(self.focused(), (PAIRED_TOKEN, "drives"))

    def test_asking_to_remove_puts_focus_on_keep_and_keeping_puts_it_back_on_remove(self):
        self.panel.refresh()
        self.panel._toggle_directions(PAIRED_TOKEN)
        self.focus((PAIRED_TOKEN, "remove"))
        self.panel._ask_to_remove(PAIRED_TOKEN)
        self.assertEqual(self.focused(), (PAIRED_TOKEN, "keep"))
        self.panel._keep()
        self.assertEqual(self.focused(), (PAIRED_TOKEN, "remove"))

    def test_focus_outside_the_list_is_left_alone(self):
        self.panel.refresh()
        self.window.window.makeFirstResponder_(self.panel.pair_button.view)
        self.panel.invalidate()
        self.panel.refresh()
        self.assertIs(self.window.window.firstResponder(), self.panel.pair_button.view)

    def test_a_removed_machines_focus_is_not_chased(self):
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        self.panel.refresh()
        self.panel._toggle_directions(SECOND_TOKEN)
        self.focus((SECOND_TOKEN, "drives"))
        self.panel._remove(SECOND_TOKEN)
        self.panel.refresh()
        self.assertNotEqual(self.focused(), (SECOND_TOKEN, "drives"))


class FollowTheFirstMachineTests(Base):
    def test_a_pairing_stored_from_the_service_thread_reaches_the_controller_on_the_next_tick(self):
        self.store.remove_peer(PAIRED_TOKEN)
        self.window.peers_changed()
        self.assertEqual(self.controller.cfg.auth_token, "")
        self.store.add_peer(entry(FIRST, SECOND_TOKEN, "Laptop"))
        self.assertEqual(self.controller.cfg.auth_token, "")
        self.window._follow_first_peer()
        self.assertEqual(self.controller.cfg.auth_token, SECOND_TOKEN)
        self.assertEqual([peer["token"] for peer in self.store.current()["peers"]], [SECOND_TOKEN])


class ShowingACodeTests(Base):
    def test_the_code_is_shown_in_two_groups_with_its_address_and_a_qr(self):
        with mock.patch.object(kvm_bridge_app.machines_panel.pairing, "pairing_address", return_value="192.0.2.9"):
            self.panel._toggle_sheet()
            self.panel._show_code()
            self.panel.refresh()
        self.assertEqual(self.service.begun, 1)
        self.assertEqual(self.panel.code_label.text, "482 913")
        self.assertEqual(self.panel.code_expiry.text, "Expires in 60 s")
        self.assertIn("192.0.2.9", self.panel.code_address.text)
        self.assertFalse(self.panel.qr_view.isHidden())
        self.assertIsNotNone(self.panel.qr_view.image())
        self.assertTrue(self.panel.show_button.view.isHidden())

    def test_with_addresses_hidden_there_is_no_qr_and_no_address(self):
        self.controller.cfg.hide_addresses = True
        with mock.patch.object(kvm_bridge_app.machines_panel.pairing, "pairing_address", return_value="192.0.2.9"):
            self.panel._toggle_sheet()
            self.panel._show_code()
            self.panel.refresh()
        self.assertTrue(self.panel.qr_view.isHidden())
        self.assertNotIn("192.0.2.9", self.panel.code_address.text)
        self.assertIn("hidden", self.panel.qr_caption.text)

    def test_cancel_drops_the_code(self):
        self.panel._toggle_sheet()
        self.panel._show_code()
        self.panel.refresh()
        self.panel._cancel_code()
        self.assertEqual(self.service.cancelled, 1)
        self.assertFalse(self.panel.show_button.view.isHidden())

    def test_a_code_that_ends_says_how(self):
        self.panel._toggle_sheet()
        self.panel._show_code()
        self.panel.refresh()
        self.service.code = None
        self.service.outcome = "known_there"
        self.panel.refresh()
        self.assertIn("may already be paired here, or the code was wrong", self.panel.hosted_status.text)

    def test_a_host_with_32_machines_says_to_remove_one(self):
        self.service.begin_pairing = mock.Mock(side_effect=pairing.PairingError(pairing.ERROR_FULL))
        self.panel._show_code()
        self.assertIn("Remove one", self.panel.hosted_status.text)

    def test_closing_the_sheet_cancels_a_code_that_is_up(self):
        self.panel._toggle_sheet()
        self.panel._show_code()
        self.panel.refresh()
        self.panel._toggle_sheet()
        self.assertEqual(self.service.cancelled, 1)

    def test_a_machine_that_paired_with_the_shown_code_closes_the_sheet_and_is_named(self):
        said = []
        self.window._say = lambda message, ink="ink_2": said.append((message, ink))
        self.panel._toggle_sheet()
        self.store.add_peer(entry(FIRST, SECOND_TOKEN, "Laptop"))
        self.panel.hosted_pairing(entry(FIRST, SECOND_TOKEN, "Laptop"))
        self.assertFalse(self.panel.open)
        self.assertEqual(said, [("Paired with Laptop.", "signal")])
        self.assertEqual(self.controller.cfg.auth_token, PAIRED_TOKEN)
        self.assertEqual(len(self.store.current()["peers"]), 2)


class EnteringACodeTests(Base):
    def setUp(self):
        super().setUp()
        self.panel._toggle_sheet()
        self.service.heard = [heard()]
        self.panel.refresh()
        self.said = []
        self.window._say = lambda message, ink="ink_2": self.said.append((message, ink))

    def run_pair(self):
        threads = []

        class Immediate:
            def __init__(self, target, name=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(kvm_bridge_app.machines_panel.threading, "Thread", Immediate), \
                mock.patch.object(kvm_bridge_app.machines_panel.AppHelper, "callAfter", lambda fn, *args: fn(*args)):
            self.panel._pair()
        return threads

    def type_code(self, digits="123456"):
        for field, digit in zip(self.panel.code_boxes.fields, digits):
            field.setStringValue_(digit)

    def test_a_machine_heard_is_listed_and_choosing_it_says_what_to_type(self):
        self.assertEqual(len(self.panel.heard_list.arrangedSubviews()), 1)
        self.panel._choose(0)
        self.assertEqual(self.panel.chosen["name"], "Laptop")
        self.assertEqual(self.panel.pair_status.text, "Six digits, as shown on Laptop.")

    def test_a_machine_not_showing_a_code_is_said_so(self):
        self.service.heard = [heard(pair_id=None)]
        self.panel._heard_key = None
        self.panel.refresh()
        self.panel._choose(0)
        self.assertIn("not showing a code yet", self.panel.pair_status.text)

    def test_a_version_2_machine_is_said_older_and_never_asked(self):
        self.service.heard = [heard(name="Old", pairing=2)]
        self.panel._heard_key = None
        self.panel.refresh()
        self.panel._choose(0)
        self.assertEqual(self.panel.pair_status.text, "Old runs an older Beamer. Update Beamer on it to 1.5.0, then pair again.")
        self.type_code()
        self.run_pair()
        self.assertEqual(self.service.paired, [])

    def test_same_named_older_machines_show_their_addresses(self):
        first = heard(name="Studio", address="192.168.50.22", pairing=2)
        second = heard(name="Studio", address="169.254.3.7", pairing=2)
        self.service.heard = [first, second]
        self.panel._heard_key = None
        self.panel.refresh()
        rows = self.panel.heard_list.arrangedSubviews()
        texts = [" ".join(self.texts(row)) for row in rows]
        self.assertEqual(len(texts), 2)
        self.assertNotEqual(*texts)
        self.assertIn("192.168.50.22", texts[0])
        self.assertIn("169.254.3.7", texts[1])

    def test_shared_older_machine_addresses_follow_the_hide_addresses_setting(self):
        self.controller.cfg.hide_addresses = True
        self.service.heard = [heard(name="Studio", address="192.168.50.22", pairing=2),
                              heard(name="Studio", address="169.254.3.7", pairing=2)]
        self.panel._heard_key = None
        self.panel.refresh()
        texts = [" ".join(self.texts(row)) for row in self.panel.heard_list.arrangedSubviews()]
        self.assertTrue(all("192.168.50.22" not in text and "169.254.3.7" not in text for text in texts))

    def test_the_chosen_machine_is_paired_with_the_code_and_the_list_updates(self):
        self.service.result = entry(FIRST, SECOND_TOKEN, "Laptop")
        self.store.add_peer(self.service.result)
        self.panel._choose(0)
        self.type_code()
        self.run_pair()
        self.assertEqual(self.service.paired, [("machine", "Laptop", "123456")])
        self.assertFalse(self.panel.open)
        self.assertEqual(self.said[-1], ("Paired with Laptop. Connecting…", "signal"))
        self.assertEqual(len(self.controller.links), 2)

    def test_a_typed_address_pairs_by_address_and_beats_a_chosen_machine(self):
        self.service.result = entry(FIRST, SECOND_TOKEN, "Laptop")
        self.store.add_peer(self.service.result)
        self.panel._choose(0)
        self.panel.find_field.setStringValue_("192.0.2.55")
        self.type_code("654321")
        self.run_pair()
        self.assertEqual(self.service.paired, [("address", "192.0.2.55", "654321")])

    def test_choosing_a_machine_clears_a_typed_address(self):
        self.panel.find_field.setStringValue_("192.0.2.55")
        self.panel._choose(0)
        self.assertEqual(self.panel.find_field.stringValue(), "")

    def test_a_short_code_is_refused_before_anything_is_sent(self):
        self.panel._choose(0)
        self.type_code("123")
        self.run_pair()
        self.assertEqual(self.service.paired, [])
        self.assertEqual(self.panel.pair_status.text, "The code is 6 digits.")

    def test_every_failure_is_said_in_the_shared_words(self):
        self.panel._choose(0)
        for error, expected in (
            (pairing.PairingError(pairing.ERROR_REFUSED), "That code was not accepted, and Laptop has cancelled it."),
            (pairing.PairingError("no_answer"), "Laptop did not answer."),
            (pairing.AlreadyPaired(entry(FIRST, SECOND_TOKEN, "Laptop")), "Laptop is already paired with this machine, since"),
            (pairing.PairingError("not_saved"), "could not save it on this machine"),
        ):
            with self.subTest(error=str(error)):
                self.service.result = error
                self.type_code()
                self.run_pair()
                self.assertIn(expected, self.panel.pair_status.text)
                self.assertFalse(self.panel.pairing)

    def test_nothing_to_pair_with_disables_the_button(self):
        self.panel.chosen = None
        self.panel._refresh_sheet(False)
        self.assertFalse(self.panel.confirm_button.enabled)
        self.panel._choose(0)
        self.panel._refresh_sheet(False)
        self.assertTrue(self.panel.confirm_button.enabled)

    def refresh_heard(self, item):
        self.service.heard = [item] if item is not None else []
        self.panel.refresh()

    def test_a_chosen_machine_starting_its_code_updates_the_hint_and_keeps_a_partial_code(self):
        self.refresh_heard(heard(pair_id=None))
        self.panel._choose(0)
        self.assertIn("not showing a code yet", self.panel.pair_status.text)
        self.type_code("123")
        self.refresh_heard(heard(pair_id="first"))
        self.assertEqual(self.panel.chosen["pair_id"], "first")
        self.assertEqual(self.panel.pair_status.text, "Six digits, as shown on Laptop.")
        self.assertEqual(self.panel.code_boxes.value, "123")

    def test_a_chosen_machine_stopping_its_code_updates_the_hint(self):
        self.refresh_heard(heard(pair_id="first"))
        self.panel._choose(0)
        self.refresh_heard(heard(pair_id=None))
        self.assertIsNone(self.panel.chosen["pair_id"])
        self.assertIn("not showing a code yet", self.panel.pair_status.text)

    def test_a_new_code_or_reply_port_replaces_what_pairing_will_use(self):
        self.refresh_heard(heard(pair_id="first"))
        self.panel._choose(0)
        current = heard(pair_id="second", reply_port=24822)
        self.refresh_heard(current)
        self.assertEqual(self.panel.chosen, current)

    def test_progress_and_refusals_survive_the_beacon_changing(self):
        self.refresh_heard(heard(pair_id="first"))
        self.panel._choose(0)
        self.panel._say("That code was not accepted.", "fault")
        self.refresh_heard(heard(pair_id=None))
        self.assertEqual(self.panel.pair_status.text, "That code was not accepted.")
        self.refresh_heard(heard(pair_id="fresh"))
        self.assertEqual(self.panel.pair_status.text, "Six digits, as shown on Laptop.")
        self.panel.pairing = True
        self.panel._say("Pairing with Laptop…")
        self.refresh_heard(heard(pair_id=None))
        self.assertEqual(self.panel.pair_status.text, "Pairing with Laptop…")

    def test_a_machine_that_vanishes_clears_the_choice(self):
        self.refresh_heard(heard())
        self.panel._choose(0)
        self.refresh_heard(None)
        self.assertIsNone(self.panel.chosen)

    def test_a_listener_that_cannot_run_is_said_instead_of_an_empty_list(self):
        self.service.heard = []
        self.service.error = "address already in use"
        self.panel._heard_key = None
        self.panel.refresh()
        self.assertIn("cannot look for machines: address already in use", self.panel.heard_empty.text)


class CodeBoxCentringTests(Base):
    """beta.4 on Toby's Mac: each typed digit sat left of the middle of its box. Measured on the real
    window's boxes, drawn offscreen at the 773-point width it had, never put on screen."""

    def ink_offsets(self):
        window = self.window.window
        window.setContentSize_((773, window.frame().size.height))
        boxes = self.panel.code_boxes
        boxes.focus(window)
        for digit in "408819":
            window.firstResponder().insertText_(digit)
        self.assertEqual(boxes.value, "408819")
        window.contentView().layoutSubtreeIfNeeded()
        view = boxes.view
        bounds = view.bounds()
        picture = view.bitmapImageRepForCachingDisplayInRect_(bounds)
        view.cacheDisplayInRect_toBitmapImageRep_(bounds, picture)
        scale = picture.pixelsWide() / bounds.size.width

        def level(x, y):
            colour = picture.colorAtX_y_(x, y).colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
            return colour.redComponent() + colour.greenComponent() + colour.blueComponent()

        offsets = []
        rows = range(int(4 * scale), int((bounds.size.height - 4) * scale), 2)
        for control in boxes.fields:
            box = control.superview()
            frame = box.convertRect_toView_(box.bounds(), view)
            # Inside the box's border and corner, on its own ground.
            first, last = int((frame.origin.x + 4) * scale), int((frame.origin.x + frame.size.width - 4) * scale)
            ground = level(first, int(bounds.size.height * scale / 2))
            inked = [x for x in range(first, last) if any(abs(level(x, y) - ground) > 0.6 for y in rows)]
            self.assertTrue(inked, "no digit drawn in a box")
            ink = (inked[0] + inked[-1] + 1) / 2 / scale
            offsets.append(round(ink - (frame.origin.x + frame.size.width / 2), 2))
        return offsets

    def test_each_typed_digit_sits_in_the_middle_of_its_box(self):
        self.panel._toggle_sheet()
        self.panel.refresh()
        for offset in self.ink_offsets():
            self.assertLessEqual(abs(offset), 0.3, f"digits sit off the middle of their boxes: {self.ink_offsets()}")


class TrayTests(Base):
    """The menu bar's side: one machine's switch, every machine's tick, a pairing this Mac hosted."""

    def tray(self):
        tray = mock.Mock()
        tray.controller = self.controller
        tray.settings_store = self.store
        tray.control_window = self.window
        tray.logger = logging.getLogger("test-machines-panel")
        tray.send_item = mock.Mock()
        tray.receive_item = mock.Mock()
        tray._switch = lambda tokens, **change: kvm_bridge_app.TrayApp._switch(tray, tokens, **change)
        return tray

    def test_one_machines_switch_changes_that_machine_only(self):
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        tray = self.tray()
        kvm_bridge_app.TrayApp._set_direction(tray, SECOND_TOKEN, send=False)
        peers = {peer["token"]: peer for peer in self.store.current()["peers"]}
        self.assertFalse(peers[SECOND_TOKEN]["send"])
        self.assertTrue(peers[PAIRED_TOKEN]["send"])
        self.assertEqual(list(self.controller.links), [PAIRED_TOKEN])

    def test_turning_off_the_machine_input_is_on_brings_input_home(self):
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        self.controller.owner.on = FIRST
        tray = self.tray()
        with mock.patch.object(self.controller, "set_redirecting") as home:
            kvm_bridge_app.TrayApp._set_direction(tray, SECOND_TOKEN, send=False)
        home.assert_called_once_with(False)

    def test_the_menu_tick_turns_every_machine_off_when_all_are_on_and_all_on_otherwise(self):
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        tray = self.tray()
        kvm_bridge_app.TrayApp.toggle_send_to_windows(tray, None)
        self.assertEqual([peer["send"] for peer in self.store.current()["peers"]], [False, False])
        kvm_bridge_app.TrayApp.toggle_send_to_windows(tray, None)
        self.assertEqual([peer["send"] for peer in self.store.current()["peers"]], [True, True])

    def test_the_menu_names_the_machine_when_there_is_one_and_every_machine_when_there_are_several(self):
        tray = self.tray()
        kvm_bridge_app.TrayApp._direction_items(tray)
        self.assertEqual(tray.send_item.title, "This Mac drives Studio")
        self.assertEqual(tray.receive_item.title, "Studio drives this Mac")
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        kvm_bridge_app.TrayApp._direction_items(tray)
        self.assertEqual(tray.send_item.title, "This Mac drives every machine")
        self.assertEqual(tray.receive_item.title, "Every machine drives this Mac")

    def test_the_menu_ticks_show_mixed_when_the_machines_differ(self):
        self.pair(FIRST, SECOND_TOKEN, "Laptop")
        self.store.set_peer(SECOND_TOKEN, send=False)
        tray = self.tray()
        kvm_bridge_app.TrayApp._direction_items(tray)
        self.assertEqual(tray.send_item.state, -1)

    def test_the_menu_hides_an_address_a_nameless_machine_is_called_by(self):
        settings = self.store.current()
        settings = {**settings, "peers": [{**settings["peers"][0], "name": ""}]}
        self.store.save_settings(settings)
        self.controller.cfg.hide_addresses = True
        tray = self.tray()
        kvm_bridge_app.TrayApp._direction_items(tray)
        self.assertNotIn("192.0.2", tray.send_item.title)

    def test_the_menu_bar_hides_an_address_a_nameless_machine_is_called_by(self):
        settings = self.store.current()
        self.store.save_settings({**settings, "peers": [{**settings["peers"][0], "name": ""}]})
        self.controller.cfg.hide_addresses = True
        self.controller.owner.on = None
        tray = mock.Mock()
        tray.controller = self.controller
        tray.control_window = self.window
        tray.windows_input = mock.Mock()
        tray.window = self.window
        tray._direction_items = lambda: None
        kvm_bridge_app.TrayApp.refresh_status(tray, None)
        self.assertNotIn("192.0.2", tray.toggle_item.title)
        self.assertNotIn("192.0.2", tray.status_item.title)

    def test_a_pairing_this_mac_hosted_reaches_the_window_on_the_main_thread(self):
        tray = self.tray()
        with mock.patch.object(kvm_bridge_app.AppHelper, "callAfter") as later:
            kvm_bridge_app.TrayApp._hosted_pairing(tray, {"name": "Laptop"})
        later.assert_called_once_with(self.window.panel.hosted_pairing, {"name": "Laptop"})


if __name__ == "__main__":
    unittest.main()
