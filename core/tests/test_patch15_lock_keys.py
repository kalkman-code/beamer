"""Run the GH64 hook regressions with no native input or GUI imports."""

import ast
import ctypes
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from core import keytable


ROOT = Path(__file__).resolve().parents[2]


def load_tests(loader, tests, pattern):
    spec = importlib.util.spec_from_file_location("patch15_capture", ROOT / "win_app" / "capture_win.py")
    capture = importlib.util.module_from_spec(spec)
    injector = NS(INJECTED_MARK=0xBEA3, SCAN_TO_US=keytable.EVDEV_US)
    with patch.dict(sys.modules, {"input_injector": injector}):
        spec.loader.exec_module(capture)
    path = Path(__file__).resolve().parents[2] / "win_app" / "tests" / "test_capture_win.py"
    body = ast.parse(path.read_text(encoding="utf-8")).body
    selected = [node for node in body if isinstance(node, ast.ClassDef)
                and node.name in {"HookPoint", "InjectedHookTests", "LockKeyHookTests"}]
    ns = {"ctypes": ctypes, "unittest": unittest, "Mock": Mock, "patch": patch,
          "capture_win": capture, "INJECTED_MARK": 0xBEA3}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), ns)
    return loader.loadTestsFromTestCase(ns["LockKeyHookTests"])
