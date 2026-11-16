"""Administrator enrolment and encrypted trust storage use synthetic keys only."""

import copy
import sys
import unittest
from pathlib import Path

from core.tests.responder_harness import B, HERE, entry
from core import protocol
if sys.platform == "win32":
    import secure_worker as worker
else:
    worker = None


@unittest.skipUnless(sys.platform == "win32", "Windows secure desktop worker tests")
class EnrolmentTests(unittest.TestCase):
    def setUp(self):
        self.source = {"machine_id": protocol.id_text(HERE), "name": "Test PC", "peers": [entry(B, "Peer")]}
        self.peer = protocol.id_text(B)

    def test_only_explicitly_selected_paired_desktops_are_granted(self):
        result = worker.make_grant(self.source, Path("C:/test/settings.json"), [self.peer])
        self.assertEqual([p["id"] for p in result["peers"]], [self.peer])
        self.assertEqual(set(result["peers"][0]), {"id", "token", "platform", "allow_drive", "in_use"})
        self.assertEqual(result["machine_id"], protocol.id_text(HERE))

    def test_empty_unknown_disabled_and_mobile_selection_rejected(self):
        for ids, changes in (([], {}), (["unknown"], {}), ([self.peer], {"allow_drive": False}),
                             ([self.peer], {"in_use": 1}), ([self.peer], {"platform": "ios"}),
                             ([self.peer], {"token": "unpaired"})):
            with self.subTest(ids=ids, changes=changes):
                source = copy.deepcopy(self.source)
                source["peers"][0].update(changes)
                with self.assertRaises(ValueError):
                    worker.make_grant(source, Path("C:/test/settings.json"), ids)

    def test_enrolment_does_not_modify_original_pairing(self):
        before = copy.deepcopy(self.source)
        worker.make_grant(self.source, Path("C:/test/settings.json"), [self.peer])
        self.assertEqual(self.source, before)

    def test_machine_dpapi_round_trip_and_tampering(self):
        clear = b"synthetic pairing material"
        encrypted = worker.protect(clear)
        self.assertNotIn(clear, encrypted)
        self.assertEqual(worker.unprotect(encrypted), clear)
        changed = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
        with self.assertRaises(OSError):
            worker.unprotect(changed)

    def test_worker_refuses_to_run_from_user_writable_source_checkout(self):
        with self.assertRaises(PermissionError):
            worker.require_installed_binary()


if __name__ == "__main__":
    unittest.main()
