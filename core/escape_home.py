"""Clock-free triple-Escape-to-come-home detection, the way hardware KVM switches do it: three
taps of Escape in quick succession while this machine drives another bring input straight back.
Only fresh presses count, never auto-repeat, and any other key in between starts the count again."""

TAPS = 3
# The longest wait between one tap and the next that still counts as the same run.
_GAP = 0.500


class EscapeTaps:
    def __init__(self, taps=TAPS, gap=_GAP):
        self.taps = int(taps)
        self.gap = float(gap)
        self.reset()

    def reset(self):
        self._count = 0
        self._last = None

    def press(self, t):
        """One fresh press of Escape at time t: True on the press that completes the run."""
        t = float(t)
        self._count = self._count + 1 if self._last is not None and t - self._last <= self.gap else 1
        self._last = t
        if self._count >= self.taps:
            self.reset()
            return True
        return False
