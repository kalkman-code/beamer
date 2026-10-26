"""Recognise a physical, bare Fn tap without changing Fn combinations.

Only keycode 63 identifies Fn itself. The secondary-Fn flag also accompanies
arrow and function keys, so the flag on its own must never start a tap.
"""

FN_CODE = 0x3F
FN_FLAG = 1 << 23
TOGGLE = "_toggle_input_language"  # Local queue item, never sent on the wire.
CHORD_FLAGS = (1 << 17) | (1 << 18) | (1 << 19) | (1 << 20)


class FnTap:
    def __init__(self, quartz):
        self.quartz = quartz
        self.pressed = set()
        self.held = False
        self.captured = False
        self.used = False

    def feed(self, event_type, event, *, enabled):
        """Return (consume Fn event, toggle on release).

        A press starting locally cannot toggle after crossing to Windows. A
        captured press ending locally is swallowed but cannot toggle either.
        """
        q = self.quartz
        if event_type not in (q.kCGEventKeyDown, q.kCGEventKeyUp, q.kCGEventFlagsChanged):
            if self.held and event_type not in (
                    q.kCGEventMouseMoved, q.kCGEventLeftMouseDragged,
                    q.kCGEventRightMouseDragged, q.kCGEventOtherMouseDragged):
                self.used = True
            return False, False
        code = int(q.CGEventGetIntegerValueField(event, q.kCGKeyboardEventKeycode))
        flags = int(q.CGEventGetFlags(event))
        if code != FN_CODE:
            if event_type == q.kCGEventKeyDown:
                self.pressed.add(code)
            elif event_type == q.kCGEventKeyUp:
                self.pressed.discard(code)
            # Other modifiers, arrows and function keys make this a chord.
            if self.held:
                self.used = True
            return False, False
        down = bool(flags & FN_FLAG) if event_type == q.kCGEventFlagsChanged else event_type == q.kCGEventKeyDown
        if down:
            if not self.held:
                self.held = True
                self.captured = enabled
                self.used = bool(self.pressed or flags & CHORD_FLAGS)
            return bool(self.captured), False
        consume = self.held and self.captured
        toggle = consume and enabled == self.captured and not self.used and not (flags & CHORD_FLAGS)
        self.held = self.captured = self.used = False
        return bool(consume), bool(toggle)
