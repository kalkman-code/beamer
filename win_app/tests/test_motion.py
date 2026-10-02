import os
import unittest

try:
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
except ImportError:  # PySide6 is only in the Windows venv
    QApplication = None


@unittest.skipIf(QApplication is None, "needs PySide6")
class MotionTest(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        import motion

        self.motion = motion
        self.real_reduced = motion.reduced
        motion.reduced = lambda: False
        self.window = QWidget()
        column = QVBoxLayout(self.window)
        self.row = QWidget()
        inner = QVBoxLayout(self.row)
        inner.addWidget(QLabel("A row that slides"))
        column.addWidget(self.row)
        column.addStretch(1)
        self.window.resize(300, 200)

    def tearDown(self):
        self.motion.reduced = self.real_reduced
        self.window.close()

    def finish(self, owner, key):
        animation = self.motion._runs(owner).get(key)
        self.assertIsNotNone(animation)
        animation.setCurrentTime(animation.duration())

    def test_nothing_moves_while_the_window_is_hidden(self):
        self.motion.set_shown(self.row, False)
        self.assertTrue(self.row.isHidden())
        self.assertFalse(self.motion.running(self.row, "shown"))

    def test_nothing_moves_under_reduced_motion(self):
        self.window.show()
        self.motion.reduced = lambda: True
        self.motion.set_shown(self.row, False)
        self.assertTrue(self.row.isHidden())
        self.assertIsNone(self.row.graphicsEffect())

    def test_a_row_slides_shut_and_its_cap_comes_off(self):
        self.window.show()
        self.motion.set_shown(self.row, False)
        self.assertFalse(self.row.isHidden(), "still on its way out")
        self.assertFalse(self.motion.target_shown(self.row))
        self.finish(self.row, "shown")
        self.assertTrue(self.row.isHidden())
        self.assertEqual(self.row.maximumHeight(), self.motion.QWIDGETSIZE_MAX)
        self.assertTrue(self.row.layout().isEnabled())
        self.assertIsNone(self.row.graphicsEffect())

    def test_a_change_of_mind_turns_the_row_round(self):
        self.window.show()
        self.motion.set_shown(self.row, False)
        self.motion.set_shown(self.row, True)
        self.finish(self.row, "shown")
        self.assertFalse(self.row.isHidden())
        self.assertEqual(self.row.maximumHeight(), self.motion.QWIDGETSIZE_MAX)

    def test_the_ring_slides_to_the_new_choice(self):
        first, second = QWidget(self.window), QWidget(self.window)
        first.setGeometry(10, 10, 40, 40)
        second.setGeometry(100, 10, 40, 40)
        ring = self.motion.Ring(self.window, lambda widget: widget.rect(), "#ffffff", 4)
        ring.follow(first)
        self.window.show()
        ring.follow(second)
        self.assertEqual(ring.current().x(), 10)
        self.finish(ring, "ring")
        self.assertEqual(ring.current().x(), 100)

    def test_the_diagram_lands_on_its_side_at_once_before_the_window_opens(self):
        import diagram

        def mac(side):
            return [{"key": "b", "label": "Mac", "side": side, "methods": ["edge"], "parts": [], "corner": "top_left",
                     "chosen": True}]

        picture = diagram.ArrangementDiagram(self.window)
        picture.set_machines(mac("left"), "Right Ctrl", "hold", False)
        picture.set_machines(mac("top"), "Right Ctrl", "hold", True)
        self.assertEqual(picture._places["b"][0], 90.0)
        self.assertEqual(picture._marks[("edge", "b", "top")], 1.0)
        self.assertEqual(picture._marks[("edge", "b", "left")], 0.0)
        self.assertEqual(picture._marks[("cap",)], 1.0)
        self.assertIn("hold Right Ctrl", picture.accessibleDescription())


if __name__ == "__main__":
    unittest.main()
