import dataclasses
import unittest
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bridge import KVMController
from bridge_fakes import PAIRED_TOKEN, FakeClock, FakeQuartz, crossing_config, quiet_logger
from fake_link import FakeLink, bring_up


class UpdateConfigWhileRedirectingTests(unittest.TestCase):
    """update_config sets self.redirecting = False directly instead of going
    through set_redirecting(False), so it skips _set_cursor_follows_mouse(True)
    when settings are applied mid-redirect."""

    def setUp(self):
        FakeQuartz.reset_cursor_spies()
        self.controller = KVMController(
            crossing_config(PAIRED_TOKEN),
            logger=quiet_logger(),
            quartz=FakeQuartz,
            clock=FakeClock(),
            link_factory=FakeLink,
            desktop_bounds=lambda: (0.0, 0.0, 1728.0, 1117.0),
        )
        self.link = bring_up(self.controller)

    def test_update_config_while_redirecting_restores_cursor_association(self):
        self.controller.set_redirecting(True)
        self.assertTrue(self.controller.redirecting)
        FakeQuartz.reset_cursor_spies()

        self.controller.update_config(crossing_config(PAIRED_TOKEN))

        self.assertFalse(self.controller.redirecting)
        self.assertIn(
            True,
            FakeQuartz.associate_calls,
            "update_config must re-enable CGAssociateMouseAndMouseCursorPosition "
            "when it forces redirecting False mid-redirect, same as set_redirecting(False) does",
        )

    def test_update_config_drops_a_link_dialled_to_an_address_that_is_no_longer_saved(self):
        cfg = self.controller.cfg
        self.link.dialled = (cfg.host, cfg.port)

        self.controller.update_config(dataclasses.replace(cfg))
        self.assertEqual(self.link.dropped, [])
        self.assertTrue(self.link.live())

        self.controller.update_config(dataclasses.replace(cfg, host="192.0.2.99"))
        self.assertEqual(self.link.dropped, ["Settings saved; reconnecting"])
        self.assertIs(self.controller._primary_link, self.link)


if __name__ == "__main__":
    unittest.main()
