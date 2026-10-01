"""This Mac's listener and its beacon use its own port setting, never a machine's."""

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bridge
import settings_store
from bridge_fakes import PAIRED_TOKEN
from fake_link import FakeLink
from wake import WakingController
from windows_input import WindowsInput


class OwnPortTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        path = Path(self.directory.name) / "config.json"
        raw = settings_store.config_to_raw(settings_store.editable_default_config())
        raw.update(host="192.0.2.20", port=9100, auth_token=PAIRED_TOKEN, pc_name="Studio")
        path.write_text(json.dumps(raw))
        self.store = settings_store.SettingsStore(path)
        self.cfg = self.store.load()
        book, identity = bridge.links_from_store(self.store, "1.5.0")
        self.controller = WakingController(self.cfg, logger=logging.getLogger("t"), book=book, identity=identity, link_factory=FakeLink)

    def test_the_port_is_the_settings_own_not_the_first_machines(self):
        # Migrated from 1.4.x, both are 9100; they part when the machines change.
        self.assertEqual(self.controller.own_port, 9100)
        settings = self.store.current()
        settings["port"] = 24820
        self.store.save_settings(settings)
        self.assertEqual(self.controller.cfg.port, 9100)
        self.assertEqual(self.controller.own_port, 24820)

    def test_the_listener_follows_the_own_port_and_stays_when_the_first_machine_changes(self):
        settings = self.store.current()
        settings["port"] = 24820
        self.store.save_settings(settings)
        wire = WindowsInput(self.controller)
        self.addCleanup(wire.server.stop)
        wire.server = mock.Mock(input_scale=None, listening=False)
        wire.sync(self.cfg)
        wire.server.start.assert_called_once_with(24820)

    def test_a_controller_built_from_a_config_alone_still_listens_where_it_dials(self):
        controller = WakingController(self.cfg, logger=logging.getLogger("t"), link_factory=FakeLink)
        self.assertEqual(controller.own_port, self.cfg.port)


if __name__ == "__main__":
    unittest.main()
