"""The Remote tasks setup in the setup window: the menu bar's front end for the guided steps.

Pairing still asks in dialogs (the pasted command, or Connect's "Finish in
your browser…"); every step after it is a page of ``menubar_wizard``'s
window. The steps themselves are ``guided_setup``'s and run on a worker
thread; :class:`WindowPrompts` hands each question to the UI thread, which
draws its page (``window_page``, plain data) and answers once a button says
so. No AppKit here.
"""

from __future__ import annotations

from openswap import printer
from openswap.setup_wizard import ACTION_CLOSE
from openswap.worker import cli, guided_setup
from openswap.worker.guided_setup import Readiness, SetupClosed, ThreadedPrompts

ACTION_CONTINUE = "continue"
ACTION_SKIP = "skip"
ACTION_YES = "yes"
ACTION_NO = "no"
ACTION_DONE = "done"
ACTION_ADD_FOLDER = "add_folder"

STEP_TITLES = ("Start the worker", "Choose the account for tasks", "Choose folders for tasks")
WORKER_BODY = "The worker runs in the background on this Mac and starts the tasks you send it from OpenTag."
FOLDERS_BODY = ("Tasks can only work in these folders, each in its own copy (a git worktree) so your "
                "files aren't touched, except folders that work in the folder itself.")
LIVE_OFF = "Live tasks stay off until the live check passes, and it can't run from the menu bar yet."
SIGN_IN_NOTE = ("A shell command in a Claude task can read or replace that account's own sign-in "
                "(macOS can't run Claude Code's sandbox inside OpenSwap's).")
SET_UP = "Remote tasks are set up"
ALMOST = "Almost done"

# The questions that draw a window page; the rest (pairing) stay dialogs.
_PAGES = frozenset({"confirm", "pick", "check_folders", "done", "hold"})
_CLOSE = {"title": "Close Setup", "action": ACTION_CLOSE}


def _note(line: str) -> dict:
    return {"kind": "note", "text": line, "tone": "warning" if line.startswith(printer.MARK_BAD) else "muted"}


def _button(title: str, action: str, enabled: bool = True) -> dict:
    return {"title": title, "action": action, "enabled": enabled}


def window_page(step: int, call: str, args: tuple, view: dict, notes) -> dict:
    """The page for a step's question: ``call`` is the Prompts method, ``view`` the owner's picks so far.

    ``view`` holds ``selected`` (the account row, or None), ``checked`` (one
    bool per folder row, then per added folder) and ``added`` (the folders
    added with Add folder…).
    """
    rows = [_note(line) for line in notes]
    page = {
        "title": STEP_TITLES[step],
        "body": None,
        "rows": rows,
        "primary": _button("Continue", ACTION_CONTINUE),
        "secondary": None,
        "skip": {"title": "Skip", "action": ACTION_SKIP},
        "back": False,
        "leading": _CLOSE,
        "progress": (step + 1, len(guided_setup.STEPS)),
    }
    if call == "confirm" and args[0] == guided_setup.WORKER_OFFER:
        page["body"] = WORKER_BODY
        page["primary"] = _button("Start worker", ACTION_YES)
    elif call == "confirm":
        page["body"] = args[0]
        page["primary"] = _button("Yes", ACTION_YES)
        page["skip"] = {"title": "No", "action": ACTION_NO}
    elif call == "pick":
        question, options = args
        page["body"] = question
        rows.append({"kind": "choices", "multiple": False, "items": [
            {"title": option, "selected": index == view["selected"], "action": f"select:{index}"}
            for index, option in enumerate(options)
        ]})
        page["primary"] = _button("Continue", ACTION_CONTINUE, view["selected"] is not None)
    elif call == "check_folders":
        options = [*args[0], *((cli.display_path(folder), "") for folder in view["added"])]
        page["body"] = FOLDERS_BODY
        rows.append({"kind": "choices", "multiple": True, "items": [
            {"title": label, "detail": note, "selected": view["checked"][index], "action": f"toggle:{index}"}
            for index, (label, note) in enumerate(options)
        ]})
        rows.append({"kind": "actions", "items": [_button("Add folder…", ACTION_ADD_FOLDER)]})
    elif call == "hold":
        page["skip"] = None
    return page


def _worker_row(state: Readiness) -> tuple[bool | None, str]:
    if guided_setup.settling(state):
        return None, "connecting…"
    if state.worker == "running" and state.paused:
        return False, "paused"
    if state.worker == "running":
        return {"revoked": (False, "removed in OpenTag"),
                "expired": (False, "pairing expired")}.get(state.connection, (True, "running"))
    return False, "on but not running" if state.worker == "stopped" else "off"


def done_page(state: Readiness) -> dict:
    """The last page: a ✓/✗ checklist, then one line per gap naming the Settings → Remote control."""
    worker_ok, worker = _worker_row(state)
    live = state.execution == "live"
    items = [
        ("Worker", worker, worker_ok),
        ("Account", state.account or "none", state.account is not None),
        ("Folders", ", ".join(state.readable) or "none",
         False if state.refused else True if state.readable else None),
        ("Live tasks", "on" if live else "off", True if live else None),
    ]
    lines = [f"{line[0].upper()}{line[1:]}." for line in state.settings_missing]
    if not lines and not live:
        lines.append(LIVE_OFF)
    lines += [f'{printer.MARK_BAD} "{workspace_id}" is blocked. {guided_setup.workspace_message(code, gui=True)}'
              for workspace_id, code in state.refused]
    if state.provider == "claude" and state.permission_override == "follow":
        lines.append(SIGN_IN_NOTE)
    return {
        "title": SET_UP if not state.settings_missing and live else ALMOST,
        "body": None,
        "rows": [{"kind": "summary", "items": items}, *(_note(line) for line in lines)],
        "primary": _button("Done", ACTION_DONE),
        "secondary": None,
        "skip": None,
        "back": False,
        "progress": (len(guided_setup.STEPS), len(guided_setup.STEPS)),
    }


def _answer(call: str, action: str, view: dict):
    """What a finishing button answers to ``call``; ``_IGNORED`` when it finishes nothing."""
    if call == "confirm" and action in (ACTION_YES, ACTION_NO, ACTION_SKIP):
        return action == ACTION_YES
    if action == ACTION_SKIP and call in ("pick", "check_folders"):
        return None
    if call == "pick" and action == ACTION_CONTINUE and view["selected"] is not None:
        return view["selected"]
    if call == "check_folders" and action == ACTION_CONTINUE:
        count = len(view["checked"]) - len(view["added"])
        return ([index for index in range(count) if view["checked"][index]]
                + [folder for index, folder in enumerate(view["added"]) if view["checked"][count + index]])
    if (call, action) in (("hold", ACTION_CONTINUE), ("done", ACTION_DONE)):
        return None
    return _IGNORED


_IGNORED = object()


class WindowPrompts(ThreadedPrompts):
    """The guided steps as setup window pages; pairing stays in ``dialogs``.

    The worker thread calls the Prompts methods. ``serve()`` (the menu bar's
    timer, UI thread) draws the page a question waits on with
    ``window.show``; the window's buttons come back through ``answer`` (UI
    thread), which redraws in place for a selection and wakes the worker
    thread for a finishing button. Lines said during a step are shown on its
    page; a step that ends with a line to read (a failure, or the folder
    results) holds its page with Continue before the next step.
    ``choose_folder()`` opens the native folder chooser (UI thread) and
    returns a path or None.
    """

    gui = True

    def __init__(self, dialogs, window, choose_folder):
        super().__init__(dialogs)
        self._window = window
        self._choose_folder = choose_folder
        self._notes: list[str] = []
        self._step: int | None = None
        self._asked: str | None = None
        self._skipped = False
        self._closed = False
        self._showing: dict | None = None
        self._view: dict = {}
        self._last: dict | None = None

    # ---- the worker thread --------------------------------------------------
    def say(self, text: str) -> None:
        with self._lock:
            self._notes.append(text)

    def section(self, title: str) -> None:
        self._hold()
        step = next(index for index, (_name, header, _next) in enumerate(guided_setup.STEPS) if header == title)
        with self._lock:
            if self._step is not None:
                self._notes = []  # the first page keeps what pairing said
            self._step, self._asked, self._skipped = step, None, False

    def confirm(self, question: str, *, default: bool = True) -> bool | None:
        return self._call("confirm", question, default=default)

    def pick(self, question: str, options, *, default: int | None = None) -> int | None:
        return self._call("pick", question, list(options), default=default)

    def check_folders(self, options, checked):
        return self._call("check_folders", list(options), list(checked))

    def done(self, state: Readiness) -> None:
        self._call("done", state)

    def flush(self) -> None:
        if self._step is None:
            super().flush()
            return
        try:
            self._hold()
        except SetupClosed:
            pass

    def waiting(self, title, message, *, cancel):
        self._to_dialogs()
        return super().waiting(title, message, cancel=cancel)

    def _to_dialogs(self) -> None:
        with self._lock:
            for line in self._notes:
                self._dialogs.say(line)
            self._notes = []

    def _call(self, name: str, *args, **kwargs):
        if name not in _PAGES:
            self._to_dialogs()
            return super()._call(name, *args, **kwargs)
        self._asked = name
        try:
            return super()._call(name, *args, **kwargs)
        finally:
            with self._lock:
                self._notes = []

    def _hold(self) -> None:
        """Before the next step: show what the last one said, unless the owner skipped it."""
        with self._lock:
            read = self._step is not None and self._notes and not self._skipped and not self._closed and (
                self._asked == "check_folders"
                or any(not line.startswith(printer.MARK_OK) for line in self._notes))
        if read:
            self._call("hold")

    # ---- the UI thread ------------------------------------------------------
    def serve(self) -> None:
        with self._lock:
            request = self._pending
            if request is not None and request["call"][0] in _PAGES:
                self._pending = None
            else:
                request = None
        if request is None:
            super().serve()
            return
        if self._closed:
            request["closed"] = True
            request["done"].set()
            return
        name, args, kwargs = request["call"]
        self._showing = request
        self._view = {"selected": kwargs.get("default"), "added": [],
                      "checked": list(args[1]) if name == "check_folders" else []}
        self._draw(self._window.show)

    def _page(self) -> dict:
        name, args, _kwargs = self._showing["call"]
        if name == "done":
            return done_page(args[0])
        with self._lock:
            notes, step = list(self._notes), self._step
        return window_page(step, name, args, self._view, notes)

    def _draw(self, draw, **extra) -> None:
        self._last = {**self._page(), **extra}
        draw(self._last)

    def answer(self, action: str) -> None:
        """A window button (``ACTION_*``, ``select:<row>``, ``toggle:<row>``)."""
        if action == ACTION_CLOSE:
            self.close()
            return
        request = self._showing
        if request is None:
            return
        name = request["call"][0]
        kind, _, row = action.partition(":")
        if kind == "select":
            self._view["selected"] = int(row)
        elif kind == "toggle":
            self._view["checked"][int(row)] = not self._view["checked"][int(row)]
        elif action == ACTION_ADD_FOLDER:
            folder = self._choose_folder()
            if not folder:
                return
            self._view["added"].append(str(folder))
            self._view["checked"].append(True)
        else:
            result = _answer(name, action, self._view)
            if result is _IGNORED:
                return
            if name == "done":
                self._window.close()
            else:
                # Until the next page: the step works (a write may take a moment).
                self._draw(self._window.render, busy=True)
            self._showing = None
            self._skipped = action in (ACTION_SKIP, ACTION_NO)
            request["result"] = result
            request["done"].set()
            return
        self._draw(self._window.render)

    def close(self) -> None:
        """Close Setup, the close button or Escape: end the setup at once.

        The question the worker thread waits on (and any later one) answers
        as Close Setup; a write already running still finishes.
        """
        self._closed = True
        request, self._showing = self._showing, None
        self._window.close()
        if request is not None:
            request["closed"] = True
            request["done"].set()

    def front(self) -> None:
        """Set up chosen again while this run is open: bring its window forward."""
        if not self._closed and self._last is not None:
            self._window.show(self._last)
