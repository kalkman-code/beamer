import unittest
from unittest import mock

import input_injector
import keyboard_language


class ForegroundTests(unittest.TestCase):
    def test_installed_layout_is_requested_on_the_foreground_window_and_confirmed(self):
        api = mock.Mock()
        api.GetForegroundWindow.return_value = 123
        api.GetWindowThreadProcessId.return_value = 456
        api.GetKeyboardLayout.side_effect = [0x4090409, 0x4190419]

        def layouts(count, values):
            if values is not None:
                values[0], values[1] = 0x4090409, 0x4190419
            return 2

        api.GetKeyboardLayoutList.side_effect = layouts
        with mock.patch.object(keyboard_language, "user32", api):
            self.assertTrue(keyboard_language.select_language("ru"))
        api.PostMessageW.assert_called_once_with(123, 0x0050, 0, 0x4190419)
        api.ActivateKeyboardLayout.assert_not_called()

    def test_character_and_position_resolution_use_the_foreground_layout(self):
        api = mock.Mock()
        api.VkKeyScanExW.return_value = 0xBE
        api.MapVirtualKeyExW.return_value = 52
        api.ToUnicodeEx.return_value = 0
        with mock.patch.object(input_injector, "user32", api), \
                mock.patch.object(input_injector, "foreground_layout", return_value=0x4190419):
            self.assertEqual(input_injector._vk_key_scan("ю"), 0xBE)
            input_injector._map_virtual_key(0xBE)
            input_injector._key_at(".")
        api.VkKeyScanExW.assert_called_once_with("ю", 0x4190419)
        self.assertEqual(api.MapVirtualKeyExW.call_args_list[0].args, (0xBE, 0, 0x4190419))
        self.assertEqual(api.ToUnicodeEx.call_args.args[-1], 0x4190419)
        api.GetKeyboardLayout.assert_not_called()

    def test_foreground_thread_not_beamers_worker_is_queried(self):
        api = mock.Mock()
        api.GetForegroundWindow.return_value = 123
        api.GetWindowThreadProcessId.return_value = 456
        api.GetKeyboardLayout.return_value = 0x4190419
        with mock.patch.object(keyboard_language, "user32", api):
            self.assertEqual(keyboard_language.current_language(), "ru")
        api.GetKeyboardLayout.assert_called_once_with(456)

    def test_an_uninstalled_language_never_loads_a_layout_or_changes_the_window(self):
        api = mock.Mock()
        api.GetForegroundWindow.return_value = 123
        api.GetKeyboardLayout.return_value = 0x4090409
        api.GetKeyboardLayoutList.return_value = 0
        with mock.patch.object(keyboard_language, "user32", api):
            self.assertFalse(keyboard_language.select_language("ru"))
        api.PostMessageW.assert_not_called()


if __name__ == "__main__":
    unittest.main()
