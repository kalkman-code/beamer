"""Every user-visible string goes through core.i18n, and the catalogues are complete.

Read from the source, not imported, so it runs on every platform without AppKit or Qt. Any prose
literal handed to any call must be marked, tr("...") or N_("..."), so that the extractor finds it,
unless the call is one that never reaches a screen: a raise, a logger, an exception class, or a
name in NOT_SHOWN. A helper that calls tr() on its argument is not enough on its own, since the
extractor only sees literals inside tr()/N_(); its callers pass N_("...") or tr("..."). A tr()
evaluated at import time, before the language is installed, fails too.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCES = ("core", "mac_app", "win_app")
MARKERS = {"tr", "trn", "trc", "N_"}
WORD = re.compile(r"[A-Za-z]{2,}")
# One lowercase word with no space is an identifier (a palette ink, a style role, a selector
# "relaunch:", a unit "ms", a D-Bus method), never a sentence a person reads.
IDENTIFIER = re.compile(r"^[a-z_][A-Za-z0-9_.:/-]*$")
LOGGERS = re.compile(r"^(_?log|_?logger|LOG|LOGGER|logging|_LOG)$")
# Exceptions carry diagnostics for the log; where one is shown, its caller shows a translated
# sentence instead.
EXCEPTION = re.compile(r"(Error|Exception|Refused|Abandoned|Warning)$")
# Text that stays English in every language, wherever it appears.
ALLOWED = {
    "Beamer",  # the product's name
}
# Calls whose string arguments are never shown: system and library names, paths, registry keys,
# HTTP/WMI selectors, log diagnostics, style values and in-memory list/struct-building operations.
NOT_SHOWN = {
    "getattr", "hasattr", "setattr", "startswith", "endswith", "join", "split", "replace", "format", "encode",
    "Thread", "ArgumentParser", "add_argument", "setStyleSheet", "setObjectName", "setProperty",
    "OpenKey", "QueryValueEx", "SetValueEx", "DeleteValue", "ExecQuery", "Dispatch", "fromData", "save",
    "filterWithName_", "AXUIElementCopyAttributeValue", "AXUIElementSetAttributeValue", "NSClassFromString",
    "has_extension", "get_extension_major", "request", "call", "get_dbus_method", "dlopen", "LoadLibrary",
    "run", "Popen", "check_output", "check_call", "compile", "match", "search", "sub", "fullmatch",
    "get", "pop", "setdefault", "environ", "Path", "open", "write", "print",
    "_warn_once", "_fail", "drop", "_connection_failed", "set_redirecting",
    # Its arguments are templates matched against shown text, already in the catalogue from where
    # that text is made.
    "matches_translation",
    "pack", "warning", "append", "__init__", "find_library", "setValue_forHTTPHeaderField_",
    "loadBundle", "lookUpClass", "protocolNamed", "normalize", "lookup", "len",
    "dispatch", "ExecMethod_", "Methods_", "persistentDomainForName_",
    "beginActivityWithOptions_reason_", "asset_path", "CreateMutexW", "OpenMutexW", "QKeySequence",
    "_turn_off_clashes", "exception", "_force_local", "stringForKey_", "open_dbus_connection", "update",
    "require_version", "_log_helper_failure",
    "report_input_problem",  # Operation names are log diagnostics; the blocker supplies translated UI text.
    # XML paths and scheduled-task run levels are Windows data, not translated prose.
    "find", "findall", "findtext", "SubElement", "task_xml",
    "_FlagAsMethod",  # COM method identifiers must retain their registered spelling.
}


def source_files():
    for folder in SOURCES:
        yield from sorted((ROOT / folder).glob("*.py"))


def _name(func) -> str:
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _owner(func) -> str:
    receiver = func.value if isinstance(func, ast.Attribute) else None
    return receiver.attr if isinstance(receiver, ast.Attribute) else receiver.id if isinstance(receiver, ast.Name) else ""


def _prose_text(text: str) -> bool:
    text = text.strip()
    return (bool(WORD.search(text)) and not IDENTIFIER.match(text) and text not in ALLOWED
            and (" " in text or text[:1].isupper()))


def _prose(node) -> bool:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _prose_text(node.value)
    if isinstance(node, ast.JoinedStr):
        return _prose_text("".join(part.value for part in node.values if isinstance(part, ast.Constant)))
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _prose(node.left) or _prose(node.right)
    if isinstance(node, ast.IfExp):
        return _prose(node.body) or _prose(node.orelse)
    return False


def _not_shown(node: ast.Call, parent, parents, exception_classes) -> bool:
    name = _name(node.func)
    if name in {"insert", "remove"} and _owner(node.func) == "environment":
        return bool(node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]*", node.args[0].value))
    if name == "__init__" and name in NOT_SHOWN:
        ancestor = parent
        while ancestor is not None and not isinstance(ancestor, ast.ClassDef):
            ancestor = parents.get(ancestor)
        return ancestor is not None and ancestor.name in exception_classes
    return (name in MARKERS or name in NOT_SHOWN or isinstance(parent, ast.Raise)
            or bool(EXCEPTION.search(name)) or bool(LOGGERS.match(_owner(node.func))))


def hard_coded(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    bases = {node.name: {_name(base) for base in node.bases} for node in ast.walk(tree)
             if isinstance(node, ast.ClassDef)}
    exception_classes = {name for name, parent_names in bases.items()
                         if parent_names & {"Exception", "BaseException"}}
    while True:
        children = {name for name, parent_names in bases.items() if parent_names & exception_classes}
        if children <= exception_classes:
            break
        exception_classes |= children
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _not_shown(node, parents.get(node), parents, exception_classes):
            continue
        for arg in list(node.args) + [keyword.value for keyword in node.keywords]:
            # Win32's class name is a system identifier; the following title remains visible text.
            if _name(node.func) == "CreateWindowExW" and len(node.args) > 1 and arg is node.args[1]:
                continue
            if _prose(arg):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{arg.lineno}: {_name(node.func)}({ast.unparse(arg)[:70]})")
    return found


def unreadable_tr(path: Path) -> list[str]:
    """tr() over text built at runtime, f"..." or "..." + x, which the extractor cannot read, so
    it never reaches the catalogue: the template goes in tr() and the values in .format()."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node.func) in MARKERS and node.args:
            text = node.args[1] if _name(node.func) == "trc" and len(node.args) > 1 else node.args[0]
            if isinstance(text, (ast.JoinedStr, ast.BinOp)):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: {ast.unparse(node)[:80]}")
    return found


def unmarked_choice_names(path: Path) -> list[str]:
    """("key", "Shown name") pairs, the shape of every choice table, whose name is not marked:
    the table is shown as it stands, so its names must reach the catalogue through N_() or tr()."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2:
            key, name = node.elts
            if (isinstance(key, ast.Constant) and isinstance(key.value, str) and (key.value == "" or IDENTIFIER.match(key.value))
                    and isinstance(name, ast.Constant) and isinstance(name.value, str) and _prose_text(name.value)
                    and name.value[:1].isupper()):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: {ast.unparse(node)[:80]}")
    return found


def import_time_tr(path: Path) -> list[str]:
    """tr() calls that run when the module is imported: at module level, in a class body, or in a
    default argument, all of which run before the language is installed."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    found = []

    def visit(node, in_function):
        if isinstance(node, ast.Call) and _name(node.func) in {"tr", "trn", "trc"} and not in_function:
            found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                visit(default, in_function)
            for child in (node.body if isinstance(node.body, list) else [node.body]):
                visit(child, True)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, in_function)

    visit(tree, False)
    return found


class I18nCoverageTests(unittest.TestCase):
    def test_win32_window_class_is_an_identifier_but_its_title_is_visible(self):
        path = ROOT / "win_app/clipboard_win.py"
        source = 'win32.CreateWindowExW(0, "STATIC", "Visible title", 0)'
        with mock.patch.object(Path, "read_text", return_value=source):
            found = hard_coded(path)
        self.assertEqual(found, ["win_app/clipboard_win.py:1: CreateWindowExW('Visible title')"])

    def test_open_mutex_name_is_internal_but_message_box_text_is_visible(self):
        path = ROOT / "win_app/portable_entry.py"
        source = (
            'ctypes.windll.kernel32.OpenMutexW(0, False, "Local\\\\Beamer.Receiver")\n'
            'ctypes.windll.user32.MessageBoxW(None, "Visible body", "Visible title", 0x10)'
        )
        with mock.patch.object(Path, "read_text", return_value=source):
            found = hard_coded(path)
        self.assertEqual(found, [
            "win_app/portable_entry.py:2: MessageBoxW('Visible body')",
            "win_app/portable_entry.py:2: MessageBoxW('Visible title')",
        ])

    def test_no_hard_coded_user_visible_strings(self):
        found = [line for path in source_files() for line in hard_coded(path)]
        self.assertEqual(found, [], "mark these with tr() or N_() from core.i18n:\n" + "\n".join(found))

    def test_translated_text_is_a_literal_the_extractor_can_read(self):
        found = [line for path in source_files() for line in unreadable_tr(path)]
        self.assertEqual(found, [], "put the template in tr() and the values in .format():\n" + "\n".join(found))

    def test_choice_tables_mark_their_names(self):
        found = [line for path in source_files() for line in unmarked_choice_names(path)]
        self.assertEqual(found, [], "mark these names with N_() and show them through tr():\n" + "\n".join(found))

    def test_no_translation_at_import_time(self):
        found = [line for path in source_files() for line in import_time_tr(path)]
        self.assertEqual(found, [], "mark these with N_() and call tr() where the text is shown:\n" + "\n".join(found))

    def test_catalogues_are_current_and_complete(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "i18n_extract.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
