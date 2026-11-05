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


# What the Remote tasks setup needs lifted from ``MenuBarApp`` (pairing dialogs, then the setup window).
SETUP_METHODS = {"_run_guided_setup", "_run_connect", "_connect_other_server", "_on_setting",
                 "_drain_guided_setup", "_setup_running", "_setup_prompts", "_on_remote_setup_action",
                 "_choose_task_folder"}


class SetupWindow:
    """The setup window: records each page; ``answers`` are the actions clicked on each new page."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.pages = []
        self.shown = []
        self.fresh = False
        self.visible = False
        self.closed = 0

    def show(self, page):
        self.pages.append(page)
        self.shown.append(page)
        self.fresh = self.visible = True

    def render(self, page):
        self.pages.append(page)

    def close(self):
        self.visible = False
        self.closed += 1


def settle_setup(app, timeout=20):
    """What on_sync_tick does: serve the setup's questions and click each new page's answers."""
    import time

    deadline = time.monotonic() + timeout
    while getattr(app, "_guided_setup", None) is not None:
        assert time.monotonic() < deadline, "the setup did not finish"
        app._drain_guided_setup()
        window = app._remote_setup_window
        if window.fresh:
            window.fresh = False
            answer = window.answers.pop(0) if window.answers else "close"
            for action in [answer] if isinstance(answer, str) else answer:
                app._on_remote_setup_action(action)
        time.sleep(0.005)
