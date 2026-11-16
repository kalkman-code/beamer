"""Load keyboard libraries without discovery utilities, a display or native input."""
import unittest
from unittest.mock import Mock, patch

from win_app import xkb_x11


class XkbLoadingTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(xkb_x11, "_libs", {})
        cache.start()
        self.addCleanup(cache.stop)

    def test_missing_discovery_uses_all_runtime_sonames_and_caches(self):
        with patch.object(xkb_x11.ctypes.util, "find_library", return_value=None), \
                patch.object(xkb_x11.ctypes, "CDLL", side_effect=lambda _: Mock()) as load:
            libraries = xkb_x11._load()
            self.assertEqual(libraries, xkb_x11._load())
        self.assertEqual([call.args[0] for call in load.call_args_list],
                         ["libxcb.so.1", "libxkbcommon.so.0", "libxkbcommon-x11.so.0"])

    def test_wayland_loads_only_xkb_and_honours_a_discovered_path(self):
        with patch.object(xkb_x11.ctypes.util, "find_library", return_value="/lib/libxkbcommon.so.0"), \
                patch.object(xkb_x11.ctypes, "CDLL", return_value=Mock()) as load:
            xkb_x11._load({"xkb": "xkbcommon"})
        load.assert_called_once_with("/lib/libxkbcommon.so.0")

    def test_loader_error_retains_dependency_diagnostic_and_allows_retry(self):
        error = OSError("libdependency.so.0 unavailable")
        with patch.object(xkb_x11.ctypes.util, "find_library", return_value=None), \
                patch.object(xkb_x11.ctypes, "CDLL", side_effect=[error, Mock()]):
            with self.assertRaises(RuntimeError) as failure:
                xkb_x11._load({"xkb": "xkbcommon"})
            self.assertIs(failure.exception.__cause__, error)
            self.assertIn(str(error), str(failure.exception))
            self.assertNotIn("xkb", xkb_x11._libs)
            self.assertEqual(1, len(xkb_x11._load({"xkb": "xkbcommon"})))
