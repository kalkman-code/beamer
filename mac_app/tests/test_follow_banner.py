"""The follow banner at every width: its words never break mid-word and Stop following keeps its size."""
import unittest
from unittest import mock

from mac_app.tests.design_offscreen import AppKit, kvm_bridge_app, offscreen_control_window, settle

PEER = "b" * 32
WIDTHS = (320, 426, 640, 900, 1440, 1920)


def follow(control):
    peer = {
        "id": PEER, "name": "Windows workstation", "platform": "windows", "token": "synthetic",
        "host": "192.0.2.30", "port": 24820, "hw": "", "send": True,
        "allow_drive": True, "side": "right", "side_set_at": 100, "side_by": PEER,
        "paired_with": [], "paired_at": 1, "linked": True, "from_1_4": False,
    }
    control.settings_store._cache["peers"] = [peer]
    control.controller.book.data["peers"] = [peer]
    control.controller.cfg.design_follow_peer = PEER
    control.controller.cfg.same_on_both = False
    control._refresh_design_follow()


class FollowBannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        kvm_bridge_app.theme.init_fonts()
        cls.control = offscreen_control_window()
        follow(cls.control)
        cls.control._select_page("design")

    def test_text_keeps_whole_words_and_button_its_natural_size(self):
        control = self.control
        label, button = control.follow_banner_label.view, control.stop_follow_button.view
        words = control.follow_banner_label.text.split()
        for width in WIDTHS:
            with self.subTest(width=width):
                control.window.setFrame_display_(((100000, 100000), (width, 900)), False)
                for _ in range(3):
                    settle(control.window)
                self.assertFalse(control.follow_banner.isHidden())
                column = label.bounds().size.width
                attrs = control.follow_banner_label.view.attributedStringValue()
                widest = max(
                    AppKit.NSAttributedString.alloc().initWithString_attributes_(
                        word, attrs.attributesAtIndex_effectiveRange_(0, None)[0]).size().width
                    for word in words)
                self.assertGreaterEqual(column, widest, "a word is wider than its column")
                natural = control.stop_follow_button.width.constant()
                self.assertAlmostEqual(button.frame().size.width, natural, delta=1)
                self.assertAlmostEqual(button.frame().size.height, 30, delta=1)
                self.assertGreater(label.frame().size.height, 0)


if __name__ == "__main__":
    unittest.main()
