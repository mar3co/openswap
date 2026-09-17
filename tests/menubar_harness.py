"""Lift menu bar methods out of their source without importing AppKit or rumps."""

import ast
from pathlib import Path


def extract_class(source_path, class_name, names, scope):
    """Compile only ``names`` from ``class_name`` into a base-less class.

    The methods resolve their globals from ``scope``, which is where each test
    injects its fakes. A requested method that no longer exists fails loudly,
    so a rename in the source cannot silently hollow out a test.
    """
    source_path = Path(source_path)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    cls = next(node for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    cls.bases = []
    cls.body = [node for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name in names]
    missing = sorted(set(names) - {node.name for node in cls.body})
    assert not missing, f"{class_name} has no method(s) {missing} in {source_path.name}"
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    exec(compile(module, str(source_path), "exec"), scope)
    return scope[class_name]
