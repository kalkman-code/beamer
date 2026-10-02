"""The Overview's list of paired machines and the pairing sheet (WIRE.md section 6).

The list shows every machine this Mac has paired, each with its state, the two directions as that
machine's own switches, and a way to remove it. The sheet is the other half of pairing: this Mac
shows a code (and the same code as a QR) for another machine to enter, or enters one shown by a
machine it hears on the network, or at an address typed in. The sentences are core/peerlist.py's,
the same on Windows.

All of it runs on the main thread. The pairing itself blocks, so it runs on a thread of its own
and comes back through AppHelper; `PairingService` calls `store` and `peers` from its socket
threads, so nothing here hops to the main thread from those.
"""

import re
import threading

import AppKit
from PyObjCTools import AppHelper

import machines
import motion
import qr
import theme
import widgets
from core import pairing, peerlist, protocol, ways as core_ways

QR_SIDE = 176
CODE_GROUP = 3


def grouped(code):
    """The six digits as two groups of three, which is how a person reads them across a room."""
    return " ".join(code[start:start + CODE_GROUP] for start in range(0, len(code), CODE_GROUP))


def seconds(count):
    return f"Expires in {count} s"


class MachinesPanel:
    def __init__(self, controller, settings_store, logger, owner):
        self.controller = controller
        self.settings_store = settings_store
        self.logger = logger
        self.owner = owner
        # Set by the app once the beacon listener exists.
        self.service = None
        self.open = False
        self.removing = None
        self.chosen = None
        self.pairing = False
        self._focus_after = None
        self.wide = True
        self._targets = []
        self._rows_key = None
        self.directions_open = set()
        self._heard_key = None
        self._heard = []
        self._showing = False
        self._shown_outcome = None
        self._hosted = None
        self._pair_ink = "ink_2"
        self._qr_for = None
        self.machines_module = self._machines_module()
        self.sheet = self._sheet()

    # Building

    def _machines_module(self):
        module = widgets.Module(spacing=10)
        module.add(widgets.eyebrow("Machines"))
        self.list = widgets.stack(spacing=8)
        module.add(self.list)
        self.empty = widgets.note("No machines paired yet.")
        module.add(self.empty.view)
        line = widgets.stack(vertical=False, spacing=10)
        self.list_note = widgets.note("Beamer connects to each machine on its own whenever both are running.")
        line.addArrangedSubview_(self.list_note.view)
        self.pair_button = widgets.action_button("Pair a machine", self._toggle_sheet, scale="small")
        line.addArrangedSubview_(self.pair_button.view)
        module.add(line)
        return module

    def _sheet(self):
        module = widgets.Module()
        module.add(widgets.eyebrow("Pair a machine"))
        self.pair_grid = widgets.stack(spacing=20)
        self.pair_grid.setAlignment_(AppKit.NSLayoutAttributeTop)
        show = self._show_half()
        enter = self._enter_half()
        self.pair_grid.addArrangedSubview_(show)
        self.pair_grid.addArrangedSubview_(enter)
        module.add(self.pair_grid)
        self.pair_stacked = [
            view.widthAnchor().constraintEqualToAnchor_(self.pair_grid.widthAnchor()) for view in (show, enter)
        ]
        return module

    def _show_half(self):
        half = widgets.stack(spacing=8)
        widgets.add(half, widgets.label("Show a code on this Mac", theme.TYPE["body"], 600))
        widgets.add(half, widgets.note(
            "On the other machine, open Beamer, choose this Mac in its list and type the code."
        ).view)
        self.show_button = widgets.action_button("Show a code", self._show_code, style="primary")
        half.addArrangedSubview_(self.show_button.view)
        self.code_label = widgets.Label("", theme.TYPE["readout"], 700, mono=True, tracking=theme.TIGHT["readout"])
        widgets.add(half, self.code_label.view)
        self.code_expiry = widgets.Label("", theme.TYPE["small"], mono=True, ink="ink_3")
        widgets.add(half, self.code_expiry.view)
        self.code_address = widgets.Label("", theme.TYPE["small"], mono=True, ink="ink_3")
        widgets.add(half, self.code_address.view)
        self.qr_view = widgets._autolayout(AppKit.NSImageView.alloc().init())
        # Whole points to a module already: scaling would soften the edges.
        self.qr_view.setImageScaling_(AppKit.NSImageScaleNone)
        self.qr_view.setImageAlignment_(AppKit.NSImageAlignTopLeft)
        self.qr_view.setAccessibilityLabel_("The pairing code as a QR code")
        widgets.size(self.qr_view, QR_SIDE, QR_SIDE)
        half.addArrangedSubview_(self.qr_view)
        self.qr_caption = widgets.note("")
        widgets.add(half, self.qr_caption.view)
        self.hosted_status = widgets.note()
        widgets.add(half, self.hosted_status.view)
        self.cancel_button = widgets.action_button("Cancel code", self._cancel_code, scale="small")
        half.addArrangedSubview_(self.cancel_button.view)
        self._show_code_widgets(False)
        return half

    def _enter_half(self):
        half = widgets.stack(spacing=8)
        widgets.add(half, widgets.label("Enter a code from another machine", theme.TYPE["body"], 600))
        heard = widgets.stack(spacing=8)
        widgets.add(heard, widgets.label("On this network", theme.TYPE["note"], 600, ink="ink_2"))
        self.heard_list = widgets.stack(spacing=1)
        self.heard_frame = widgets.box("rule")
        self.heard_frame.addSubview_(self.heard_list)
        widgets.pin(self.heard_list, self.heard_frame, (1, 1, 1, 1))
        widgets.add(heard, self.heard_frame)
        self.heard_empty = widgets.note()
        widgets.add(heard, self.heard_empty.view)
        find_box, self.find_field = widgets.field()
        find_secret_box, self.find_secret = widgets.field(secure=True)
        find_secret_box.setHidden_(True)
        self.find_boxes = (find_box, find_secret_box)
        for control in (self.find_field, self.find_secret):
            control.setPlaceholderAttributedString_(widgets.attributed("Address", theme.TYPE["small"], ink="ink_3", mono=True))
            control.setAccessibilityLabel_("Address of a machine that is not listed")
            control.setTarget_(self._target(self._address_typed))
            control.setAction_("fire:")
        heard.setCustomSpacing_afterView_(14, self.heard_empty.view)
        heard.setCustomSpacing_afterView_(14, self.heard_frame)
        widgets.add(heard, widgets.label("Not listed? Type its address.", theme.TYPE["note"], ink="ink_2"))
        find_line = widgets.stack(vertical=False, spacing=8)
        for box in self.find_boxes:
            find_line.addArrangedSubview_(widgets.hug(box, AppKit.NSLayoutPriorityDefaultLow))
        widgets.add(heard, find_line)
        widgets.add(half, heard)
        widgets.add(half, widgets.label("Code shown on the other machine", theme.TYPE["note"], 600, ink="ink_2"))
        self.code_boxes = widgets.CodeBoxes(pairing.CODE_DIGITS, lambda _control: self._pair())
        widgets.add(half, self.code_boxes.view)
        line = widgets.stack(vertical=False, spacing=10)
        line.setAlignment_(AppKit.NSLayoutAttributeTop)
        self.pair_status = widgets.note()
        line.addArrangedSubview_(self.pair_status.view)
        self.confirm_button = widgets.action_button("Pair", self._pair, style="primary")
        line.addArrangedSubview_(self.confirm_button.view)
        widgets.add(half, line)
        self._say("Choose a machine, or type its address, then type the code it shows.")
        return half

    def _target(self, callback):
        target = widgets.callback_target(callback)
        self._targets.append(target)
        return target

    # Reading the window's state

    def _shown(self, text):
        return self.owner._shown(text)

    def rows(self):
        """The list's rows, from the controller and the settings as they stand now."""
        controller = self.controller
        peers = controller.book.peers()
        names = peerlist.labels(peers)
        inbound = self.owner.inbound_ids()
        rows = []
        for entry in peers:
            link = controller.links.get(entry["token"])
            ident = entry.get("id") or None
            rows.append(machines.row(
                entry, names[entry["token"]],
                live=bool(link is not None and link.live()),
                kind=link.kind if link is not None else "none",
                status=link.status if link is not None else "",
                here=ident is not None and controller.owner.on == ident,
                driving=ident is not None and controller.driver == ident,
                inbound=ident is not None and ident in inbound,
                has_link=link is not None,
            ))
        return rows

    # The list

    def refresh(self):
        rows = self.rows()
        hide = self.controller.cfg.hide_addresses
        # Not the sentence under each state: a link that is still trying changes it every attempt,
        # and a rebuild takes keyboard and VoiceOver focus off whatever it was on.
        key = (hide, self.removing, self.wide, tuple(
            (r.token, r.label, r.platform, r.address, r.state.key, r.state.word, r.state.tone, r.state.led,
             r.in_use, r.drives, r.driven)
            for r in rows))
        key += (tuple(sorted(self.directions_open)),)
        if key != self._rows_key:
            self._rows_key = key
            self._build_rows(rows)
        self.empty.view.setHidden_(bool(rows))
        self.list_note.view.setHidden_(not rows)
        wanted = self.open or not rows
        motion.set_hidden(self.sheet.view, not wanted)
        self.pair_button.set_title("Cancel" if self.open and rows else "Pair a machine")
        self.pair_button.view.setHidden_(not rows)
        if wanted:
            self._refresh_sheet(hide)
        elif self._showing:
            self._cancel_code()

    def _build_rows(self, rows):
        focus = self._focus_after or self._focused_control()
        self._focus_after = None
        for view in list(self.list.arrangedSubviews()):
            self.list.removeArrangedSubview_(view)
            view.removeFromSuperview()
        for row in rows:
            widgets.add(self.list, self._row_view(row))
        if focus is not None:
            self._focus(focus)

    def _focused_control(self):
        """The id of the list control that has keyboard focus, or None."""
        view = self.owner.window.firstResponder()
        if view is None or not hasattr(view, "isDescendantOf_") or not view.isDescendantOf_(self.list):
            return None
        while view is not None and getattr(view, "focus_id", None) is None:
            view = view.superview()
        return getattr(view, "focus_id", None)

    def _focus(self, ident):
        """Gives focus to the control rebuilt under `ident`, as a rebuild takes it off the old one."""
        stack = [self.list]
        while stack:
            view = stack.pop()
            if getattr(view, "focus_id", None) == ident:
                self.owner.window.makeFirstResponder_(view)
                return
            stack.extend(view.subviews())

    def _row_view(self, row):
        state = row.state
        frame = widgets.box("ground", "rule", theme.RADIUS["field"])
        label = self._shown(row.label)
        frame.setAccessibilityLabel_(f"{label}, {self._shown(state.word)}")
        body = widgets.stack(spacing=6)
        head = widgets.stack(vertical=False, spacing=9)
        led = widgets.LED()
        led.set(state.led, state.blink)
        head.addArrangedSubview_(led.view)
        name = widgets.Label(label, theme.TYPE["body"], 600)
        head.addArrangedSubview_(widgets.hug(widgets.squeeze(name.view), AppKit.NSLayoutPriorityDefaultLow))
        tag = widgets.Label(self._shown(state.word), theme.TYPE["eyebrow"], 700, state.tone, tracking=theme.TRACKING["eyebrow"], upper=True)
        head.addArrangedSubview_(tag.view)
        widgets.add(body, head)
        remove = self._removal(row)
        where = "  ".join((row.platform, self._shown(row.address)))
        place = widgets.stack(vertical=False, spacing=10)
        place.addArrangedSubview_(widgets.hug(widgets.squeeze(widgets.Label(where, theme.TYPE["small"], mono=True, ink="ink_3").view),
                                              AppKit.NSLayoutPriorityDefaultLow))
        if not self.wide and self.removing != row.token:
            place.addArrangedSubview_(remove)
        widgets.add(body, place)
        widgets.add(body, widgets.note(self._shown(state.detail)).view)
        in_use = widgets.Switch("In use", on_change=lambda on, t=row.token: self._direction(t, in_use=on))
        in_use.value = row.in_use
        in_use.view.focus_id = (row.token, "in_use")
        drives = widgets.Switch("This Mac drives it", on_change=lambda on, t=row.token: self._direction(t, send=on))
        drives.value = row.drives
        drives.view.focus_id = (row.token, "drives")
        driven = widgets.Switch("It drives this Mac", on_change=lambda on, t=row.token: self._direction(t, allow_drive=on))
        driven.value = row.driven
        driven.view.focus_id = (row.token, "driven")
        opened = row.token in self.directions_open
        disclosure = self._chip("Hide directions" if opened else "Directions", lambda t=row.token: self._toggle_directions(t),
                                f"{'Hide' if opened else 'Show'} directions for {label}",
                                (row.token, "directions"))
        widgets.add(body, self._controls(row, in_use, disclosure, drives, driven, remove))
        frame.addSubview_(body)
        widgets.pin(body, frame, (10, 12, 10, 12))
        return frame

    def _chip(self, text, callback, label, focus_id=None):
        chip = widgets.pressable(callback, "well", "edge", theme.RADIUS["field"])
        chip.focus_id = focus_id
        chip.setAccessibilityLabel_(label)
        word = widgets.Label(text, theme.TYPE["small"], 600, ink="ink")
        chip.addSubview_(word.view)
        widgets.pin(word.view, chip, (6, 12, 6, 12))
        return chip

    def _controls(self, row, in_use, disclosure, drives, driven, remove):
        """The machine switch leads; its two direction switches stay under their disclosure."""
        if self.removing == row.token:
            column = widgets.stack(spacing=6)
            for view in (remove,):
                widgets.add(column, view)
            return column
        line = widgets.stack(vertical=False, spacing=16)
        line.addArrangedSubview_(widgets.hug(in_use.view, AppKit.NSLayoutPriorityRequired))
        line.addArrangedSubview_(widgets.hug(widgets.box(), AppKit.NSLayoutPriorityDefaultLow))
        line.addArrangedSubview_(disclosure)
        if self.wide:
            line.addArrangedSubview_(remove)
        column = widgets.stack(spacing=4)
        column.addArrangedSubview_(line)
        if row.token in self.directions_open:
            directions = widgets.stack(vertical=False, spacing=24)
            for view in (drives.view, driven.view):
                directions.addArrangedSubview_(view)
                widgets.hug(view, AppKit.NSLayoutPriorityRequired)
            column.addArrangedSubview_(directions)
        return column

    def _toggle_directions(self, token):
        if token in self.directions_open:
            self.directions_open.remove(token)
        else:
            self.directions_open.add(token)
        self.refresh()

    def _removal(self, row):
        label = self._shown(row.label)
        if self.removing != row.token:
            return self._chip("Remove", lambda t=row.token: self._ask_to_remove(t), f"Remove {label}", (row.token, "remove"))
        line = widgets.stack(vertical=False, spacing=8)
        words = widgets.note(f"Remove {label}? It stays paired on that machine until you remove this one there too.", ink="ink")
        line.addArrangedSubview_(words.view)
        line.addArrangedSubview_(self._chip("Remove", lambda t=row.token: self._remove(t), f"Confirm removing {label}", (row.token, "confirm")))
        line.addArrangedSubview_(self._chip("Keep", self._keep, f"Keep {label}", (row.token, "keep")))
        return line

    def _ask_to_remove(self, token):
        # Focus goes to Keep: pressing Space twice must not remove a machine.
        self.removing = token
        self._focus_after = (token, "keep")
        self.refresh()

    def _keep(self):
        token, self.removing = self.removing, None
        self._focus_after = (token, "remove")
        self.refresh()

    def _remove(self, token):
        self.removing = None
        before = core_ways.way_back_state(self.settings_store.current())
        try:
            removed = self.settings_store.remove_peer(token)
        except Exception as exc:
            self.logger.warning("could not remove a machine: %s", exc)
            self.owner._say(f"Could not remove it: {exc}", "fault")
            self.refresh()
            return
        if removed is not None:
            self.logger.info("removed %s", removed.get("name") or "a machine")
        self.owner.peers_changed()
        after = core_ways.way_back_state(self.settings_store.current())
        changed_peers = [peer for peer, state in after.items() if before.get(peer) != state]
        for peer in changed_peers:
            self.owner._tell(peer)

    def _direction(self, token, **change):
        handler = self.owner.direction_handler
        if handler is not None:
            handler(token, **change)
        self.refresh()

    # The sheet

    def _toggle_sheet(self):
        self.open = not self.open
        if not self.open:
            self._leave_sheet()
        self.refresh()

    def _leave_sheet(self):
        self.code_boxes.clear()
        self.chosen = None
        self._heard_key = None
        self._say("Choose a machine, or type its address, then type the code it shows.")

    def _refresh_sheet(self, hide):
        self._refresh_code(hide)
        self._refresh_heard()
        self.confirm_button.set_enabled(not self.pairing and self._wanted() is not None)

    def invalidate(self):
        """Draw the rows again from the settings on the next refresh."""
        self._rows_key = None

    def stop_showing(self):
        """The window closed: a code nobody can see does not stay up."""
        if self._showing or (self.service is not None and self.service.code is not None):
            self._cancel_code()
        self.open = False

    def _say(self, message, ink="ink_2"):
        self._pair_ink = ink
        self.pair_status.set(self._shown(message), ink=ink)

    # Showing a code

    def _show_code_widgets(self, showing):
        self.show_button.view.setHidden_(showing)
        for view in (self.code_label.view, self.code_expiry.view, self.code_address.view, self.qr_view,
                     self.qr_caption.view, self.cancel_button.view):
            view.setHidden_(not showing)

    def _show_code(self):
        service = self.service
        if service is None:
            return
        try:
            service.begin_pairing()
        except pairing.PairingError as exc:
            self.hosted_status.set(peerlist.pairing_error_text(exc, "this machine"), ink="fault")
            self.hosted_status.view.setHidden_(False)
            return
        self._hosted = None
        self._shown_outcome = None
        self._qr_for = None
        self.hosted_status.set("")
        self.refresh()

    def _cancel_code(self):
        if self.service is not None:
            self.service.cancel_pairing()
        self._showing = False
        self._shown_outcome = None
        self.hosted_status.set("")
        self._show_code_widgets(False)

    def _refresh_code(self, hide):
        service = self.service
        code = service.code if service is not None else None
        if code is not None:
            self._showing = True
            self._show_code_widgets(True)
            self.code_label.set(grouped(code))
            self.code_expiry.set(seconds(service.seconds_left))
            address = pairing.pairing_address()
            qr_text = service.qr_text(address)
            if hide:
                self.code_address.set(self._shown(f"{address} · port {service.tcp_port}") if address else "")
                self._set_qr(None, "The QR is hidden while addresses are hidden.")
            elif qr_text is None:
                self.code_address.set(f"{address} · port {service.tcp_port}" if address else "No network address found")
                self._set_qr(None, f"No QR: {service.tcp_error}" if service.tcp_error else "No QR while this Mac has no address.")
            else:
                self.code_address.set(f"{address} · port {service.tcp_port}")
                self._set_qr(qr_text, "The same code as a QR, for a phone to scan.")
            self.hosted_status.view.setHidden_(not self.hosted_status.text)
            return
        if self._showing:
            self._showing = False
            self._show_code_widgets(False)
            self._shown_outcome = service.outcome if service is not None else None
            known = service.known if service is not None else None
            text = peerlist.host_outcome_text(self._shown_outcome, self._hosted if self._shown_outcome == "paired" else known)
            ink = "signal" if self._shown_outcome == "paired" else "fault"
            self.hosted_status.set(text, ink=ink)
        self.hosted_status.view.setHidden_(not self.hosted_status.text)

    def _set_qr(self, text, caption):
        self.qr_caption.set(caption)
        self.qr_view.setHidden_(text is None)
        if text == self._qr_for:
            return
        self._qr_for = text
        self.qr_view.setImage_(qr.image(text, QR_SIDE) if text is not None else None)

    def hosted_pairing(self, entry):
        """A machine paired with the code this Mac showed (on the main thread, after the entry was stored)."""
        self._hosted = entry
        self.open = False
        self.owner.peers_changed()
        self.owner._say(peerlist.host_outcome_text("paired", entry), "signal")

    # Entering a code

    def _address_text(self):
        field = self.find_secret if self.controller.cfg.hide_addresses else self.find_field
        return field.stringValue().strip()

    def _address_typed(self):
        if self._address_text():
            self.chosen = None
            self._heard_key = None
        self._refresh_heard()
        self.code_boxes.focus(self.owner.window)

    def _wanted(self):
        """What a code would be entered for: the address typed, else the machine chosen, else None."""
        address = self._address_text()
        if address:
            return ("address", address)
        if self.chosen is not None:
            return ("machine", self.chosen)
        return None

    def _refresh_heard(self):
        service = self.service
        heard = service.machines() if service is not None else []
        chosen = self.chosen["address"] if self.chosen is not None else None
        hide = self.controller.cfg.hide_addresses
        key = (chosen, hide, tuple(tuple(sorted((k, str(v)) for k, v in item.items())) for item in heard))
        if key == self._heard_key:
            return
        self._heard_key = key
        self._heard = heard
        current = next((item for item in heard if item["address"] == chosen), None)
        if current is not None and current != self.chosen:
            showed = self.chosen["pair_id"]
            self.chosen = current
            fault = self._pair_ink == "fault"
            if current["pair_id"] != showed and not self.pairing and (current["pair_id"] or not fault):
                self._say_choice()
        for view in list(self.heard_list.arrangedSubviews()):
            self.heard_list.removeArrangedSubview_(view)
            view.removeFromSuperview()
        for index, (item, shared) in enumerate(zip(heard, peerlist.sharing_a_name(heard))):
            self._heard_row(index, item, item["address"] == chosen, shared)
        if service is not None and service.error:
            self.heard_empty.set(f"Beamer cannot look for machines: {service.error}", ink="fault")
        else:
            self.heard_empty.set(
                "Looking for machines running Beamer. Open it on the other machine and it appears here; "
                "across a VPN or on guest Wi-Fi it may not, so type its address below.", ink="ink_2")
        self.heard_frame.setHidden_(not heard)
        self.heard_empty.view.setHidden_(bool(heard))
        if chosen is not None and chosen not in {item["address"] for item in heard}:
            self.chosen = None
            self._heard_key = None

    def _heard_row(self, index, item, picked, shared_name):
        older = item.get("pairing") != pairing.PAIRING_V3
        showing = item["pair_id"] is not None and not older
        row = widgets.pressable(lambda index=index: self._choose(index), "well" if picked else "ground",
                                role=AppKit.NSAccessibilityRadioButtonRole)
        row.ring_inset = 1.0
        platform = machines.platform_name(item.get("platform") or "")
        row.setAccessibilityLabel_(self._shown(f"{item['name']}, {item['address']}")
                                   + (", older Beamer" if older else ", showing a code" if showing else ""))
        line = widgets.stack(vertical=False, spacing=9)
        led = widgets.LED()
        led.set("amber" if older else "signal" if showing else "off")
        line.addArrangedSubview_(led.view)
        name = widgets.Label(self._shown(item["name"]), theme.TYPE["note"], 600 if picked else 400)
        line.addArrangedSubview_(widgets.hug(widgets.squeeze(name.view), AppKit.NSLayoutPriorityDefaultLow))
        # A current machine's row always says where it is; an older one's only where its name is shared.
        where = self._shown(item["address"]) if shared_name or not older else ""
        detail = " · ".join(part for part in ("Older Beamer" if older else platform, where) if part)
        line.addArrangedSubview_(widgets.Label(detail, theme.TYPE["small"], mono=True, ink="amber" if older else "ink_3").view)
        row.addSubview_(line)
        widgets.pin(line, row, (7, 10, 7, 10))
        widgets.add(self.heard_list, row)

    def _choose(self, index):
        if not 0 <= index < len(self._heard):
            return
        self.chosen = self._heard[index]
        for box_field in (self.find_field, self.find_secret):
            box_field.setStringValue_("")
        self.code_boxes.clear()
        self.code_boxes.view.setAccessibilityLabel_(self._shown(f"Code shown on {self.chosen['name']}"))
        self._say_choice()
        self._heard_key = None
        self._refresh_heard()
        self.code_boxes.focus(self.owner.window)

    def _say_choice(self):
        chosen = self.chosen
        if chosen.get("pairing") != pairing.PAIRING_V3:
            self._say(peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_VERSION), chosen["name"]), "fault")
        elif chosen["pair_id"] is None:
            self._say(f"{chosen['name']} is not showing a code yet. Show one in Beamer on it first.")
        else:
            self._say(f"Six digits, as shown on {chosen['name']}.")

    def _pair(self):
        wanted = self._wanted()
        service = self.service
        if wanted is None or self.pairing or service is None:
            return
        code = re.sub(r"\D", "", self.code_boxes.value)
        if len(code) != pairing.CODE_DIGITS:
            self._say(f"The code is {pairing.CODE_DIGITS} digits.", "fault")
            return
        kind, target = wanted
        if kind == "machine":
            # The list may have refreshed since the row was chosen; pair with the latest beacon.
            target = next((item for item in self._heard if item["address"] == target["address"]), target)
            name = target["name"]
            if target.get("pairing") != pairing.PAIRING_V3:
                self.chosen = target
                self._say_choice()
                return
            if target["pair_id"] is None:
                self._say(peerlist.pairing_error_text(pairing.PairingError(pairing.ERROR_NOT_PAIRING), name), "fault")
                return
            run = lambda: service.pair(target, code)
        else:
            name = "That machine"
            run = lambda: service.pair_by_address(target, code)
        self.pairing = True
        self.confirm_button.set_enabled(False)
        self._say(self._shown(f"Pairing with {name}…" if kind == "machine" else f"Pairing with {target}…"))

        def work():
            try:
                result = run()
            except pairing.PairingError as exc:
                result = exc
            except Exception as exc:
                self.logger.exception("pairing failed")
                result = pairing.PairingError(str(exc))
            AppHelper.callAfter(self._finished, name, result)

        threading.Thread(target=work, name="pair", daemon=True).start()

    def _finished(self, name, result):
        self.pairing = False
        if isinstance(result, pairing.PairingError):
            self._say(self._shown(peerlist.pairing_error_text(result, name)), "fault")
            return
        # The name the exchange proved, not the one a beacon claimed.
        proved = result.get("name") or name
        self.code_boxes.clear()
        for box_field in (self.find_field, self.find_secret):
            box_field.setStringValue_("")
        self.chosen = None
        self._heard_key = None
        self.open = False
        self._say("Choose a machine, or type its address, then type the code it shows.")
        self.owner.peers_changed()
        self.owner._say(f"Paired with {proved}. Connecting…", "signal")
        self.logger.info("paired with %s", proved)
