import json
import os
import tempfile
import unittest
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import config


class PartialKeyMapOverrideTest(unittest.TestCase):
    def test_single_entry_matching_legacy_positional_value_is_not_discarded(self):
        """A deliberate one-entry override ("cmd": "cmd") happens to equal the legacy
        positional value for that key, so `not all(...)` is False and the override is
        silently dropped in favour of the full semantic default."""
        raw = {
            "host": "192.168.1.3",
            "port": 24820,
            "auth_token": "t",
            "key_map": {"cmd": "cmd"},
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(raw, f)
            cfg = config.load_config(path)
        finally:
            os.unlink(path)

        self.assertEqual(
            cfg.key_map["cmd"],
            "cmd",
            "user's deliberate override was discarded and replaced with the semantic default",
        )


if __name__ == "__main__":
    unittest.main()
