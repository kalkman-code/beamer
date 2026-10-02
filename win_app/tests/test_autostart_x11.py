import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import autostart_x11

EXE = "/home/alex/Beamer/beamer"


def read_exec(path: Path) -> list:
    """The Exec value as the spec's reader would see it: the string-type escapes undone first, then
    the quoting rules, then %% as a literal percent. Kept apart from the writer's code on purpose."""
    value = next(line[5:] for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("Exec="))
    unescaped, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            unescaped.append({"n": "\n", "t": "\t", "r": "\r", "s": " ", "\\": "\\"}.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            unescaped.append(value[i])
            i += 1
    value, args, current, quoted, have, i = "".join(unescaped), [], [], False, False, 0
    while i < len(value):
        char = value[i]
        if quoted:
            if char == "\\" and i + 1 < len(value):
                current.append(value[i + 1])
                i += 1
            elif char == '"':
                quoted = False
            else:
                current.append(char)
        elif char == '"':
            quoted, have = True, True
        elif char == " ":
            if have or current:
                args.append("".join(current))
            current, have = [], False
        else:
            current.append(char)
        i += 1
    if have or current:
        args.append("".join(current))
    return [arg.replace("%%", "%") for arg in args]


class AutostartX11Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.entry = self.base / "autostart" / "beamer.desktop"

    def enable(self, exe=EXE):
        autostart_x11.set_enabled(True, exe, base=self.base)

    def test_enable_writes_the_exact_file(self):
        self.enable()
        self.assertEqual(
            self.entry.read_text(encoding="utf-8"),
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Beamer\n"
            f"Exec={EXE} --hidden\n"
            "X-GNOME-Autostart-enabled=true\n"
            "NoDisplay=true\n"
            "Comment=Shares one keyboard and mouse between your computers\n",
        )

    def test_enabled_after_enable_and_disabled_after_disable(self):
        self.assertFalse(autostart_x11.is_enabled(base=self.base))
        self.enable()
        self.assertTrue(autostart_x11.is_enabled(base=self.base))
        autostart_x11.set_enabled(False, EXE, base=self.base)
        self.assertFalse(self.entry.exists())
        self.assertFalse(autostart_x11.is_enabled(base=self.base))

    def test_disabling_when_absent_is_a_no_op(self):
        autostart_x11.set_enabled(False, EXE, base=self.base)
        self.assertFalse((self.base / "autostart").exists())

    def test_enabling_twice_rewrites_in_place(self):
        self.enable()
        self.enable("/opt/beamer/beamer")
        self.assertEqual(read_exec(self.entry), ["/opt/beamer/beamer", "--hidden"])
        self.assertEqual(os.listdir(self.entry.parent), ["beamer.desktop"])

    def test_hidden_true_reads_as_disabled(self):
        self.enable()
        text = self.entry.read_text(encoding="utf-8")
        self.entry.write_text(text + "Hidden=true\n", encoding="utf-8")
        self.assertFalse(autostart_x11.is_enabled(base=self.base))
        self.entry.write_text(text + "Hidden=false\n", encoding="utf-8")
        self.assertTrue(autostart_x11.is_enabled(base=self.base))

    def test_gnome_switch_off_reads_as_disabled(self):
        self.enable()
        text = self.entry.read_text(encoding="utf-8").replace("Autostart-enabled=true", "Autostart-enabled=false")
        self.entry.write_text(text, encoding="utf-8")
        self.assertFalse(autostart_x11.is_enabled(base=self.base))

    def test_hidden_in_another_group_does_not_count(self):
        self.enable()
        text = self.entry.read_text(encoding="utf-8")
        self.entry.write_text(text + "\n[Desktop Action x]\nHidden=true\n", encoding="utf-8")
        self.assertTrue(autostart_x11.is_enabled(base=self.base))

    def test_enable_after_hidden_restores_it(self):
        self.enable()
        self.entry.write_text(self.entry.read_text(encoding="utf-8") + "Hidden=true\n", encoding="utf-8")
        self.enable()
        self.assertTrue(autostart_x11.is_enabled(base=self.base))

    def test_plain_path_is_not_quoted(self):
        self.assertEqual(autostart_x11.exec_line(EXE), f"{EXE} --hidden")

    def test_path_with_a_space_is_quoted(self):
        self.assertEqual(autostart_x11.exec_line("/opt/My Apps/beamer"), '"/opt/My Apps/beamer" --hidden')
        self.enable("/opt/My Apps/beamer")
        self.assertEqual(read_exec(self.entry), ["/opt/My Apps/beamer", "--hidden"])

    def test_dollar_is_escaped_for_both_layers(self):
        # Quoting layer \$, then the string layer doubles its backslash.
        self.assertEqual(autostart_x11.exec_line("/opt/a$b/beamer"), '"/opt/a\\\\$b/beamer" --hidden')
        self.enable("/opt/a$b/beamer")
        self.assertEqual(read_exec(self.entry), ["/opt/a$b/beamer", "--hidden"])

    def test_every_reserved_character_survives(self):
        for char in " \t\"'\\<>~|&;$*?#()`":
            with self.subTest(char=repr(char)):
                exe = f"/opt/a{char}b/beamer"
                self.enable(exe)
                self.assertEqual(read_exec(self.entry), [exe, "--hidden"])

    def test_percent_is_doubled(self):
        self.assertEqual(autostart_x11.exec_line("/opt/100%/beamer"), "/opt/100%%/beamer --hidden")
        self.enable("/opt/100%/beamer")
        self.assertEqual(read_exec(self.entry), ["/opt/100%/beamer", "--hidden"])

    @unittest.skipIf(sys.platform == "win32", "Windows has no POSIX permission bits")
    def test_the_file_is_readable_by_other_programs(self):
        self.enable()
        self.assertEqual(stat.S_IMODE(self.entry.stat().st_mode), 0o644)

    def test_xdg_config_home_is_honoured(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.base / "xdg")}):
            autostart_x11.set_enabled(True, EXE)
            self.assertTrue((self.base / "xdg" / "autostart" / "beamer.desktop").exists())
            self.assertTrue(autostart_x11.is_enabled())
            autostart_x11.set_enabled(False, EXE)
            self.assertFalse(autostart_x11.is_enabled())

    def test_config_home_defaults_to_dot_config(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch("pathlib.Path.home", return_value=self.base):
            self.assertEqual(autostart_x11.config_home(), self.base / ".config")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), mock.patch("pathlib.Path.home", return_value=self.base):
            self.assertEqual(autostart_x11.config_home(), self.base / ".config")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/dir"}), mock.patch("pathlib.Path.home", return_value=self.base):
            self.assertEqual(autostart_x11.config_home(), self.base / ".config")

    def test_an_unwritable_folder_raises_oserror_with_a_reason(self):
        (self.base / "autostart").write_text("in the way", encoding="utf-8")
        with self.assertRaises(OSError) as caught:
            self.enable()
        self.assertIn("autostart", str(caught.exception))
        self.assertEqual(os.listdir(self.base), ["autostart"])

    def test_a_failed_write_leaves_no_temp_file(self):
        self.enable()
        with mock.patch("os.replace", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaises(OSError):
                self.enable("/opt/other/beamer")
        self.assertEqual(os.listdir(self.entry.parent), ["beamer.desktop"])
        self.assertEqual(read_exec(self.entry), [EXE, "--hidden"])

    def test_installed_exe_is_only_set_when_frozen(self):
        self.assertIsNone(autostart_x11.installed_exe())
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(autostart_x11.installed_exe(), sys.executable)


if __name__ == "__main__":
    unittest.main()
