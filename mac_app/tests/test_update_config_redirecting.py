import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge import KVMController
from test_bridge import FakeClock, FakeQuartz, FakeSocket, crossing_config, link, quiet_logger


class UpdateConfigWhileRedirectingTests(unittest.TestCase):
    """update_config sets self.redirecting = False directly instead of going
    through set_redirecting(False), so it skips _set_cursor_follows_mouse(True)
    when settings are applied mid-redirect."""

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.controller = KVMController(
            crossing_config(),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            desktop_bounds=lambda: (0.0, 0.0, 1728.0, 1117.0),
        )
        link(self.controller)

    def test_update_config_while_redirecting_restores_cursor_association(self):
        self.controller.set_redirecting(True)
        self.assertTrue(self.controller.redirecting)
        FakeQuartz.reset_cursor_spies()

        self.controller.update_config(crossing_config())

        self.assertFalse(self.controller.redirecting)
        self.assertIn(
            True,
            FakeQuartz.associate_calls,
            "update_config must re-enable CGAssociateMouseAndMouseCursorPosition "
            "when it forces redirecting False mid-redirect, same as set_redirecting(False) does",
        )


if __name__ == "__main__":
    unittest.main()
