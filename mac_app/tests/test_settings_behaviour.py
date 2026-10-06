"""Mac settings decisions with in-memory configuration and no AppKit window."""

from core.tests.settings_behaviour import SettingsBehaviourTests as _SettingsBehaviourTests


class MacSettingsBehaviourTests(_SettingsBehaviourTests):
    platform = "mac"


del _SettingsBehaviourTests
