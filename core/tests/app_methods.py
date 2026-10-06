"""Run app decisions without constructing windows, installing hooks or loading user settings."""

import ast
from pathlib import Path


def load_methods(relative_path, class_name, names, namespace):
    path = Path(__file__).resolve().parents[2] / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    methods = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for method in methods:
        method.decorator_list = []
    module = ast.Module(body=methods, type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return type(class_name, (), {name: namespace[name] for name in names})
