"""The guided Remote tasks setup shared by ``openswap worker pair`` and the menu bar.

After pairing, the owner is walked through the same steps everywhere: start the
worker, confirm the Codex or Claude account, pick the folders where sessions work, then a
summary of what is still missing before Slack can start tasks on this Mac. Each step
uses the same functions as the matching ``openswap worker`` command; the
front end only supplies prompts (``Prompts``). Pairing has already succeeded
when these run, and nothing here can undo or fail it.
"""

from __future__ import annotations

import os
import re
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from openswap import pathid, printer
from openswap.exceptions import ClaudeSwitchError
from openswap.settings import load_worker_settings


class Prompts(Protocol):
    """What a front end supplies. ``interactive`` False means print next steps only."""

    interactive: bool

    def say(self, text: str) -> None: ...

    def section(self, title: str) -> None:
        """A step header (``Step 2 of 4 · Account``); plain ``say`` when a front end has nothing better."""

    def confirm(self, question: str, *, default: bool = True) -> bool | None:
        """Yes/no; ``None`` when the owner gave no answer (EOF, closed dialog)."""

    def ask(self, question: str, *, default: str = "") -> str | None:
        """Free text, stripped; ``None`` when the owner gave no answer."""

    def choose_folder(self, question: str) -> str | None:
        """A folder path; "" or ``None`` when the owner is done choosing."""

    # Optional: ``pick(question, options, *, default=None) -> int | None`` shows
    # ``options`` as a list to choose one from (``default`` selected, nothing
    # when None) and returns the chosen index, ``None`` when skipped. Without
    # it, steps number the options and ``ask`` for the number.
    #
    # The setup window adds ``gui = True`` (lines name the Settings → Remote
    # control that does a thing, never a command), ``check_folders(options,
    # checked)`` (see ``choose_folders``), ``changing_folders()`` (a context
    # manager around the folder step, from reading the saved folders until
    # its changes are written) and ``done(state)`` (the final checklist, from
    # a ``Readiness``).


def _section(ui, title: str) -> None:
    section = getattr(ui, "section", None)
    (section or ui.say)(title)


@dataclass
class TerminalPrompts:
    """``input()``/``print`` prompts for the CLI; ``read_line`` is injectable for tests.

    Only this front end styles anything (a bold step header when colors are
    on): the step text itself stays plain, because the menu bar shows the same
    lines in dialogs.
    """

    interactive: bool
    read_line: Callable[[str], str] | None = None
    write: Callable[[str], None] = field(default=print)
    # One home-folder scan shared by every folder search in a run.
    _index: object = field(default=None, repr=False)

    def say(self, text: str) -> None:
        # The one `Next:` line is bold on a color terminal, like every other command's.
        if text.startswith("Next: "):
            text = printer.next_step(text.removeprefix("Next: "))
        self.write(text)

    def section(self, title: str) -> None:
        self.write("")
        self.write(printer.heading(title))

    def _read(self, prompt: str) -> str | None:
        try:
            return (self.read_line or input)(prompt)
        except (EOFError, KeyboardInterrupt):
            self.write("")
            return None

    def confirm(self, question: str, *, default: bool = True) -> bool | None:
        answer = self._read(f"{question} {'[Y/n]' if default else '[y/N]'} ")
        if answer is None:
            return None
        answer = answer.strip().lower()
        if not answer:
            return default
        # Anything but an explicit yes is a no: never act on an unclear answer.
        return answer in {"y", "yes"}

    def ask(self, question: str, *, default: str = "") -> str | None:
        answer = self._read(f"{question} [{default}]: " if default else f"{question}: ")
        if answer is None:
            return None
        return answer.strip() or default

    def choose_folder(self, question: str) -> str | None:
        if self.read_line is not None:
            return self.ask(question)
        from openswap.folder_picker import pick_folder

        return pick_folder(f"{question}: ")

    def can_search_folders(self) -> bool:
        """Whether ``search_folders`` runs the live picker (a real terminal, no scripted input)."""
        if self.read_line is not None:
            return False
        from openswap import folder_picker

        return folder_picker.available()

    def search_folders(self, question: str, *, pinned=(), highlight: int | None = None) -> str | None:
        """The type-to-search picker with ``pinned`` numbered suggestions; "" finishes."""
        from openswap import folder_picker

        if self._index is None:
            self._index = folder_picker.FolderIndex(_cli().home_folder()).start()
        return folder_picker.pick_folder(f"{question}: ", pinned=pinned, highlight=highlight,
                                         index=self._index)


# Copy shared by the steps and their fallbacks. Each step says at most one
# short line of context and ends with at most one `Next:` command.
START_WORKER_NEXT = "Next: `openswap worker enable` to start the worker."
ACCOUNT_NEXT = "Next: `openswap worker account <slot>` to pick an account."
FOLDER_NEXT = "Next: `openswap worker workspace add --work <folder>` to add a folder."
# What the folders the owner picks are for, in one place: where remote
# Claude or Codex sessions are launched and work (owner decision 2026-10-08).
FOLDER_USE = "Folders where remote sessions can work"
FOLDER_COPY = "Each task gets its own copy (a git worktree), so your files aren't touched."
EXECUTION_OFF_NOTE = "Next: `openswap worker live-check` to turn on live tasks."
CLAUDE_EXECUTION_OFF_NOTE = (
    "Next: `openswap worker claude pin`, `openswap worker claude prepare`, then "
    "`openswap worker live-check --provider claude` to turn on live tasks."
)
READY_NOTE = f"{printer.MARK_OK} Ready: Slack can send tasks to this Mac."
# The setup window's line for each `Next:` above: the Settings → Remote control instead.
SETTINGS_NEXT = {
    START_WORKER_NEXT: "Turn on Local worker in Settings → Remote to start it.",
    ACCOUNT_NEXT: "Pick one under Account in Settings → Remote.",
    FOLDER_NEXT: "Add one under Folders in Settings → Remote.",
}


def _gui(ui) -> bool:
    return getattr(ui, "gui", False) is True


def _next(ui, text: str) -> str:
    return SETTINGS_NEXT[text] if _gui(ui) else text


# The live check refuses while the worker takes tasks: a running, unpaused worker pauses first.
PAUSE_FIRST = "`openswap worker pause`, then "


def execution_off_note(provider: str | None, *, pause_first: bool = False) -> str:
    """The one `Next:` line for turning on live tasks with the pinned account's provider.

    ``pause_first`` when the worker is running and taking tasks: the live
    check refuses until it is paused (``worker pause --off`` resumes after).
    """
    note = CLAUDE_EXECUTION_OFF_NOTE if provider == "claude" else EXECUTION_OFF_NOTE
    if pause_first:
        note = "Next: " + PAUSE_FIRST + note.removeprefix("Next: ")
    return note


def _provider_name(choice) -> str:
    return "Claude" if getattr(choice, "provider", "codex") == "claude" else "Codex"


WORKER_OFFER = "Start the worker now?"
WORKER_ONLINE = f"{printer.MARK_OK} Worker started. This Mac shows online in about 15 seconds."
WORKER_STOPPED = f"{printer.MARK_BAD} The worker is on but not running."
WORKER_PAUSED = ("The worker is running but paused, so it takes no new tasks. "
                 "Turn off Pause new tasks in Settings → Remote to resume.")
WORKER_UNREADABLE = f"{printer.MARK_BAD} Couldn't read the worker's status."
WORKER_RESTART = "turn Local worker off and on in Settings → Remote to restart it"
# The setup window's words for `enable` refusals whose message names a command.
_SETTINGS_ENABLE_MESSAGES = {
    "worker_running_unmanaged": "Could not start the worker: another one started from Terminal is running.",
}
_RUNNING_STATES = frozenset({"starting", "running"})
_MAX_ATTEMPTS = 3
_MAX_PICKS = 16


def _cli():
    # Late import: the CLI module imports this one, and tests patch its functions.
    from openswap.worker import cli
    return cli


def offer_worker(root: Path, ui: Prompts) -> None:
    """Offer to start the worker, through `worker enable`'s own function.

    Enrollment never enables local execution by itself: the worker starts only
    when the owner says yes. An already enabled worker is never toggled.
    """
    cli = _cli()
    policy = load_worker_settings(root)
    later = _next(ui, START_WORKER_NEXT)
    restart = False
    if policy.enabled is True:
        unreadable = False
        try:
            process = cli.read_status(root).get("process_state")
        except Exception:
            process, unreadable = None, True
        if process in _RUNNING_STATES:
            ui.say(WORKER_PAUSED if _gui(ui) and policy.paused is True else f"{printer.MARK_OK} Worker running.")
            return
        if not _gui(ui):
            ui.say("The worker is on but not running. Next: `openswap worker enable`.")
            return
        # The window offers to start it again (`enable` is idempotent); Settings' switch is already on.
        ui.say(WORKER_UNREADABLE if unreadable else WORKER_STOPPED)
        restart, later = True, f"{WORKER_RESTART[0].upper()}{WORKER_RESTART[1:]}."
    if not ui.interactive:
        ui.say(START_WORKER_NEXT)
        return
    if ui.confirm(WORKER_OFFER) is not True:
        ui.say(f"Not started. {later}")
        return
    try:
        cli.enable_worker(root)
    except ClaudeSwitchError as exc:
        code = str(exc)
        message = enable_message(code) if _gui(ui) else cli._enable_failure_message(exc)
        if code in cli._ENABLE_DIAGNOSTICS:
            message = f"{message.rstrip('.')} ({code})."
        # Restarting can't help while a worker from Terminal runs.
        ui.say(message if restart and code == "worker_running_unmanaged" else f"{message} {later}")
    except Exception:
        ui.say(f"Could not start the worker. {later}")
    else:
        ui.say(WORKER_ONLINE)


def account_menu(choices, pinned=None) -> tuple[list, list[str]]:
    """The numbered account menu: the eligible slots (Codex first) and their lines.

    The number is the menu position, not the slot: both providers may use
    the same slot numbers, and only one of them could keep its own. The slot
    stays visible in its own column, next to the provider.
    """
    menu = [c for c in choices.codex if c.eligible] + [c for c in choices.claude if c.eligible]
    rows = []
    for number, choice in enumerate(menu, start=1):
        current = pinned is not None and choice.account_ref == pinned.account_ref
        notes = []
        if current:
            notes.append("current")
        if choice.disabled:
            notes.append("out of rotation")
        rows.append((f"{printer.mark(True if current else None)} {number}", _provider_name(choice),
                     choice.email or "(no email)", f"({choice.alias})" if choice.alias else "",
                     f"slot {choice.number}", ", ".join(notes)))
    return menu, printer.columns(rows)


def account_options(menu) -> list[str]:
    """One row per account for a popup: the nickname first, then the email and provider."""
    rows = []
    for choice in menu:
        parts = [choice.alias] if choice.alias else []
        parts += [choice.email or "(no email)", _provider_name(choice)]
        if choice.disabled:
            parts.append("out of rotation")
        rows.append(" · ".join(parts))
    # The same nickname and email under one provider: the slot tells them apart.
    return [f"{row} · slot {choice.number}" if rows.count(row) > 1 else row
            for row, choice in zip(rows, menu)]


# The setup window's words for pin refusals whose message names a command.
_SETTINGS_ACCOUNT_MESSAGES = {
    "claude_not_supported": "That Claude account can't be picked here. Pick another.",
    "account_not_found": "That account is gone. Pick another.",
    "account_ambiguous": "That matches several accounts. Pick another.",
    "account_not_allowlisted": "Tasks can't pick that account yet. Pick another.",
    "account_is_default": "That is the pinned account already.",
    "account_not_eligible": "That account can't be used for tasks. Pick another.",
}
NO_ACCOUNT = "No account to pick. Next: `openswap codex add` or `openswap add`."
NO_ACCOUNT_SETTINGS = "No account to pick. Add a Claude or Codex account in OpenSwap first."


def account_message(code: str, *, gui: bool = False) -> str:
    """Why a pin was refused: the CLI's line, or the setup window's when ``gui``."""
    cli = _cli()
    text = cli._ACCOUNT_MESSAGES.get(code, f"Could not pin that account ({code}).")
    return _SETTINGS_ACCOUNT_MESSAGES.get(code, text) if gui else text


STATUS_READ_FAILED = "Couldn't read the worker status after the change."
# The change is saved (and admission follows the saved value), but the worker didn't confirm it.
_PAUSE_UNCONFIRMED = {
    "pause": "New tasks are paused, but the worker didn't confirm it.",
    "resume": "New tasks can start again, but the worker didn't confirm it.",
}
CONTROL_FAILED = "That change didn't go through."
# What each Settings → Remote control failed to do; a known reason follows after a colon.
_CONTROL_FAILED = {
    "enable": "Couldn't turn on the worker",
    "disable": "Couldn't turn off the worker",
    "pause": "Couldn't pause new tasks",
    "resume": "Couldn't resume new tasks",
    "stop": "Couldn't stop the task",
    "unpair": "Couldn't unpair this Mac",
}
# Reasons the CLI's tables don't have, or word for the terminal.
_CONTROL_REASONS = {
    ("disable", "worker_stop_unconfirmed"): "the worker hasn't stopped yet; try again in a moment",
    ("disable", "worker_state_unavailable"): "the worker's state could not be read",
    ("pause", "worker_state_unavailable"): "the worker's state could not be read",
    ("resume", "worker_state_unavailable"): "the worker's state could not be read",
    ("stop", "job_identity_unknown"): "the running task could not be identified",
    ("stop", "job_not_found"): "that task has already ended",
    ("stop", "active_job_mismatch"): "a different task is running now",
    ("pause", "settings_unavailable"): "the worker couldn't apply it",
    ("resume", "settings_unavailable"): "the worker couldn't apply it",
    ("unpair", "job_running"): "a task is running; stop it or let it finish first",
    ("unpair", "worker_state_unavailable"): "the worker's state could not be read",
    ("unpair", "pairing_in_progress"): "OpenSwap is busy with another change; try again in a moment",
    ("unpair", "settings_unavailable"): "the settings could not be saved",
    ("unpair", "pairing_changed"): "it was paired again meanwhile; check where it is linked and try again",
}


def control_message(code: str, action: str | None = None) -> str:
    """Why a Settings → Remote control did not do what was asked.

    ``action`` is the control used: enable, disable, pause, resume, stop,
    account or unpair; the same code can mean different things for different controls.
    """
    cli = _cli()
    if (action, code) == ("unpair", "key_left"):
        return UNPAIR_KEY_LEFT
    if code == "status_read_failed":
        return STATUS_READ_FAILED
    if code == "worker_lifecycle_busy":
        return cli.BUSY_MESSAGE
    if action == "account":
        return account_message(code, gui=True) if code in cli._ACCOUNT_MESSAGES else CONTROL_FAILED
    if action == "enable" and code in cli._ENABLE_DIAGNOSTICS:
        return enable_message(code)
    if code == "pause_unconfirmed" and action in _PAUSE_UNCONFIRMED:
        return _PAUSE_UNCONFIRMED[action]
    if action not in _CONTROL_FAILED:
        return CONTROL_FAILED
    reason = _CONTROL_REASONS.get((action, code))
    if reason is None and action == "disable" and code in cli._DISABLE_BLOCKED:
        reason = cli._DISABLE_BLOCKED[code][0]
    if reason is None and action == "stop":
        reason = cli._STOP_MESSAGES.get(code)
    return f"{_CONTROL_FAILED[action]}: {reason}." if reason else f"{_CONTROL_FAILED[action]}."


def enable_message(code: str) -> str:
    """Why the worker didn't start, in the setup window's and Settings' words."""
    return _SETTINGS_ENABLE_MESSAGES.get(code) or _cli()._enable_failure_message(ClaudeSwitchError(code))


def confirm_account(root: Path, ui: Prompts) -> None:
    """Show the eligible accounts as a numbered menu; Enter keeps the pinned one."""
    cli = _cli()
    try:
        choices = cli.worker_account_choices(root)
    except Exception:
        choices = None
    pinned = choices.pinned if choices is not None and not choices.pinned_missing else None
    if pinned is not None and not ui.interactive:
        ui.say(f"{printer.MARK_OK} Account: {_provider_name(pinned)} {pinned.label()}")
        return
    if not ui.interactive or choices is None:
        ui.say(_next(ui, ACCOUNT_NEXT))
        return
    if choices.pinned_missing:
        ui.say("Your pinned account is gone; pick another.")
    menu, lines = account_menu(choices, pinned)
    if not menu:
        ui.say(NO_ACCOUNT_SETTINGS if _gui(ui) else NO_ACCOUNT)
        return
    # Skipping after declining the current pin cancels the change: say the
    # previous account stays selected rather than implying none is pinned.
    later = (f"Kept {_provider_name(pinned)} {pinned.label()}." if pinned is not None
             else f"Skipped. {_next(ui, ACCOUNT_NEXT)}")
    current = next((str(n) for n, c in enumerate(menu, start=1)
                    if pinned is not None and c.account_ref == pinned.account_ref), "")
    pick = getattr(ui, "pick", None)
    if pick is not None:
        # A popup of nicknames, the current account selected; its row is the menu number.
        options = account_options(menu)

        def answer_once():
            index = pick("Tasks run on the account you pick.", options,
                         default=int(current) - 1 if current else None)
            return None if index is None else str(index + 1)
    else:
        ui.say("Tasks run on the account you pick.")
        for line in lines:
            ui.say(line)
        # A number, an email, an alias or `claude:<slot>` all work.
        question = "Account" if current else f"Account (1-{len(menu)}, Enter skips)"

        def answer_once():
            return ui.ask(question, default=current)
    for _attempt in range(_MAX_ATTEMPTS):
        answer = answer_once()
        if not answer:
            ui.say(later)
            return
        if answer == current:
            ui.say(f"{printer.MARK_OK} Kept {_provider_name(pinned)} {pinned.label()}")
            return
        selector = answer
        # ASCII only: str.isdigit() also accepts digits such as "²" that int() rejects.
        if answer.isascii() and answer.isdigit():
            if not 1 <= int(answer) <= len(menu):
                ui.say(f"Type a number from 1 to {len(menu)}.")
                continue
            selector = menu[int(answer) - 1].account_ref
        try:
            chosen = cli.set_worker_account(root, selector)
        except cli.AccountPinError as exc:
            ui.say(account_message(exc.code, gui=_gui(ui)))
            continue
        except Exception:
            ui.say(f"Could not pick that account. {_next(ui, ACCOUNT_NEXT)}")
            return
        ui.say(f"{printer.MARK_OK} {_provider_name(chosen)} {chosen.label()}")
        return
    ui.say(later)


def _display_path(path: Path) -> str:
    return _cli().display_path(path)


def _describe(workspace) -> str:
    """``id (label)``; the mode only when it is the advanced direct one."""
    direct = getattr(workspace, "work_root", None) is not None and workspace.mode == "direct"
    return f"{workspace.workspace_id} ({workspace.display_label}{', direct' if direct else ''})"


def _folder_of(workspace) -> list[Path]:
    work = getattr(workspace, "work_root", None)
    return [work] if work is not None else list(workspace.readonly_roots)


def folder_lines(workspaces) -> list[str]:
    """One numbered, aligned line per folder: ID, label, then the path (local only)."""
    return printer.columns([
        (f"{printer.MARK_OK} {number}", workspace.workspace_id, f'"{workspace.display_label}"',
         ", ".join(_display_path(source) for source in _folder_of(workspace)),
         "direct" if getattr(workspace, "work_root", None) is not None and workspace.mode == "direct" else "")
        for number, workspace in enumerate(workspaces, start=1)
    ])


def _readable(workspaces) -> list:
    """The folders the owner picked: work folders (and read-only folders from before)."""
    return [w for w in workspaces if w.readonly_roots or getattr(w, "work_root", None) is not None]


def _picked(root: Path, workspaces) -> list:
    """The picked folders as tasks see them: a folder of repos as its repos."""
    try:
        return _readable(_cli().launchable_workspaces(root, workspaces))
    except Exception:
        return _readable(workspaces)


def _say_folders(ui, workspaces) -> None:
    ui.say("Folders:")
    for line in folder_lines(workspaces):
        ui.say(line)


def suggested_folder_id(folder: Path, taken: set[str]) -> str:
    """A free workspace ID from a folder's name (see ``cli.suggested_folder_id``)."""
    return _cli().suggested_folder_id(folder, taken)


# The setup window's and Settings' words for workspace refusals: shorter, and never naming a command.
_SETTINGS_WORKSPACE_MESSAGES = {
    "workspace_not_found": "That folder was already removed.",
    "folder_permissions": ("Only you may open a results folder. Remove other people's access in "
                           "Finder (Get Info), then try again."),
    "readonly_source_permissions": ("Other users can change that read-only folder. Remove their write "
                                    "access in Finder (Get Info)."),
    "readable_permissions": "Other users can change it. Remove their write access in Finder (Get Info).",
    "readable_system": "That's a system folder.",
    "readable_home": "That's your whole home folder. Pick one inside it.",
    "readable_private": "That's app data or a hidden folder.",
    "readable_exposes_credentials": "It holds OpenSwap, Codex or Claude sign-ins.",
    "readable_results": "That's where results are saved.",
    "readable_unavailable": "It doesn't exist or can't be opened.",
    "readable_unsafe": "That's not a folder.",
    "readable_not_owned": "That folder isn't yours.",
    "work_not_a_repo": "No git repos in it.",
    "work_no_repos": "It holds no git repos anymore.",
    "work_overlaps_readable": "It overlaps a read-only folder. Remove that one under Folders first.",
    "mode_invalid": "A folder either gives each task its own copy or works in the folder itself.",
    "mode_not_work": "Only a folder tasks work in has a mode.",
}


def workspace_message(code: str, *, gui: bool = False) -> str:
    """Why a folder change was refused: the CLI's line, or the setup window's when ``gui``."""
    text = _cli()._WORKSPACE_MESSAGES.get(code, f"Could not approve that folder ({code}).")
    return _SETTINGS_WORKSPACE_MESSAGES.get(code, text) if gui else text


def _clause(message: str) -> str:
    """A sentence as a clause after ``<folder>: ``: lowercase start, no final period."""
    return message[:1].lower() + message[1:].rstrip(".")


def _paths_and_use(workspace) -> tuple[str, str]:
    """A saved folder's paths and how tasks use it ("" for a folder tasks work in a copy of)."""
    paths = ", ".join(_display_path(source) for source in _folder_of(workspace))
    if not paths:
        return _display_path(workspace.output_root), "results only"
    if getattr(workspace, "work_root", None) is None:
        return paths, "read only"
    return paths, "works in the folder itself" if workspace.mode == "direct" else ""


def folder_label(workspace) -> str:
    """A saved folder as the window lists it: its paths, then how tasks use it."""
    paths, use = _paths_and_use(workspace)
    return f"{paths} ({use})" if use else paths


_SHORT_USE = {"works in the folder itself": "in place"}


def settings_folders(root: Path, workspaces) -> list[tuple[str, str, str, str]]:
    """Settings → Remote's folder list: ``(ID, name, ~ paths, short use)`` per saved folder.

    The name is the folder's label, else its first folder's name. Empty
    while the registry is still only the untouched built-in ``research``
    folder: the first add replaces it.
    """
    if _cli().is_builtin_default_registry(root, workspaces):
        return []
    out = []
    for workspace in workspaces:
        paths, use = _paths_and_use(workspace)
        first = (_folder_of(workspace) or [workspace.output_root])[0]
        out.append((workspace.workspace_id, workspace.label or Path(first).name, paths, _SHORT_USE.get(use, use)))
    return out


@dataclass(frozen=True)
class FolderMenu:
    """The folder step's numbered list: detected code folders, then ones already readable."""

    folders: tuple[Path, ...]
    lines: tuple[str, ...]
    recommended: int | None  # menu index of the recommended GitHub folder
    readable: frozenset[Path]  # folders a workspace already reads
    notes: tuple[str, ...] = ()  # per folder: "recommended", "git repo", "added as <id>"


def folder_menu(root: Path, workspaces, *, groups: bool = False) -> FolderMenu:
    """Number the detected folders (a GitHub folder first, "recommended"); ✓ marks readable ones.

    ``groups`` (the setup window): a folder whose subfolders hold repos
    (``repo_groups``) counts as a code folder too, and only a code folder is
    recommended.
    """
    cli = _cli()
    try:
        detected = cli.detect_code_folders(root)
    except Exception:
        detected = []
    code = {folder: (_holds_repos(folder) or bool(repo_groups(folder))) if groups else _has_repos(folder)
            for folder in detected}
    # Repos and folders of repos first: a session needs one to work in.
    detected.sort(key=lambda folder: not code[folder])
    reads = {}
    for workspace in _readable(workspaces):
        for source in _folder_of(workspace):
            reads.setdefault(Path(source), workspace.workspace_id)
    folders = list(detected) + [source for source in reads if source not in detected]
    recommended = (0 if detected and cli.is_github_folder(detected[0]) and (code[detected[0]] or not groups)
                   else None)
    parents = set(detected)
    rows, all_notes = [], []
    for index, folder in enumerate(folders):
        notes = []
        if index == recommended:
            notes.append("recommended")
        if folder.parent in parents and folder in detected:
            notes.append("git repo")
        if folder in reads:
            notes.append(f"added as {reads[folder]}")
        note = f"({', '.join(notes)})" if notes else ""
        all_notes.append(note)
        rows.append((f"{printer.mark(True if folder in reads else None)} {index + 1}", _display_path(folder), note))
    return FolderMenu(tuple(folders), tuple(printer.columns(rows)), recommended, frozenset(reads),
                      tuple(all_notes))


def _has_repos(folder: Path) -> bool:
    """Whether ``folder`` is a git repo or holds one directly (a cheap check: no git run)."""
    if os.path.lexists(Path(folder) / ".git"):
        return True
    try:
        return any(os.path.lexists(child / ".git") for child in Path(folder).iterdir()
                   if not child.name.startswith("."))
    except OSError:
        return False


def _holds_repos(folder: Path) -> bool:
    """Whether ``folder`` is a git repo or holds one directly, by ``add_work_folder``'s rule."""
    from openswap.worker import worktrees

    return worktrees.is_repo(folder) or bool(worktrees.child_repos(folder))


def repo_groups(folder: Path) -> list[Path]:
    """The subfolders directly inside ``folder`` that hold git repos (``~/GitHub/<org>``), by name.

    Hidden folders and symlinks are skipped, as ``worktrees.child_repos`` skips them.
    """
    from openswap.worker import worktrees

    try:
        children = sorted(Path(folder).iterdir(), key=lambda p: (p.name.lower(), p.name))
    except OSError:
        return []
    out = []
    for child in children[:worktrees._MAX_SCANNED_CHILDREN]:
        try:
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
                continue
        except OSError:
            continue
        if worktrees.child_repos(child):
            out.append(child)
    return out


def _names_a_path(text: str) -> bool:
    """Whether ``text``, with a leading ``~`` for the home folder, names an existing path."""
    if text == "~" or text.startswith("~/"):
        candidate = _cli().home_folder() / text[2:].lstrip("/")
    else:
        candidate = Path(text)
    try:
        return candidate.exists()
    except (OSError, ValueError):
        return False


def parse_folder_choice(answer: str, count: int) -> list[int] | str | None:
    """Menu numbers (``1 3`` or ``1,3``; 0-based, in order, once each), a path, or ``None`` if invalid.

    An answer of numbers only is a selection; anything else is one folder
    path (a path dragged into Terminal may come shell-escaped).
    """
    text = answer.strip()
    if not text:
        return None
    tokens = [token for token in re.split(r"[\s,]+", text) if token]
    # ASCII only: str.isdigit() also accepts digits such as "²" that int() rejects.
    if all(token.isascii() and token.isdigit() for token in tokens):
        picks = []
        for token in tokens:
            number = int(token)
            if not 1 <= number <= count:
                return None
            if number - 1 not in picks:
                picks.append(number - 1)
        return picks
    # A folder that exists as typed is that folder: apostrophes in real names
    # (O'Neil's, Kid's Stuff) are never shell syntax.
    if _names_a_path(text):
        return text
    # A path dragged into Terminal comes shell-quoted or backslash-escaped
    # ('/x/Bob'\''s', /x/My\ Code): when it is one shell word, that word is
    # the path. On Windows a backslash is the path separator, so only plain
    # surrounding quotes are removed there.
    if os.name != "nt" and (text[0] in "'\"" or "\\" in text):
        try:
            words = shlex.split(text)
        except ValueError:
            words = []
        if len(words) == 1:
            return words[0]
    if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def choose_folders(root: Path, ui: Prompts) -> None:
    """Ask which folders remote sessions may work in; each becomes a workspace.

    Results go to ``~/OpenSwap Research/<id>``, created owner-only without
    asking. Enter takes the recommended GitHub folder (else the first one
    found), or keeps the current folders when some are readable already. The
    setup window (``check_folders``) shows saved and found folders as
    checkboxes instead (see ``apply_folders``).
    """
    if getattr(ui, "check_folders", None) is not None:
        with ui.changing_folders():
            _check_folders(root, ui, load_worker_settings(root).workspaces)
        return
    policy = load_worker_settings(root)
    current = _readable(policy.workspaces)
    if not ui.interactive:
        if current:
            _say_folders(ui, _picked(root, policy.workspaces))
        else:
            ui.say(FOLDER_NEXT)
        return
    ui.say(f"{FOLDER_USE}. {FOLDER_COPY}")
    if getattr(ui, "can_search_folders", lambda: False)():
        _search_folders(root, ui, bool(current))
        return
    menu = folder_menu(root, policy.workspaces)
    if menu.folders:
        for line in menu.lines:
            ui.say(line)
        if current:
            default, question = "", "Folders (Enter keeps current)"
        else:
            default, question = str((menu.recommended or 0) + 1), "Folders (numbers or a path)"
    else:
        default = ""
        question = ("Type the path to your code folder, for example ~/GitHub "
                    f"({'Enter keeps current' if current else 'Enter skips'})")
    later = "Kept the current folders." if current else f"No folder added. {FOLDER_NEXT}"
    for _attempt in range(_MAX_ATTEMPTS):
        if menu.folders:
            answer = ui.ask(question, default=default)
        else:
            # Nothing to number: the terminal's folder search or the menu
            # bar's native chooser (#88) finds it; both fall back to typing.
            answer = (getattr(ui, "choose_folder", None) or ui.ask)(question)
        if not answer:
            ui.say(later)
            return
        choice = parse_folder_choice(answer, len(menu.folders))
        if choice is None:
            ui.say(_bad_choice(len(menu.folders)))
            continue
        picked = [menu.folders[index] for index in choice] if isinstance(choice, list) else [choice]
        if _add_folders(root, ui, picked):
            return
    ui.say(later)


@dataclass(frozen=True)
class FolderRow:
    """One checkbox on the setup window's folder step."""

    label: str
    note: str
    checked: bool
    workspace: object = None  # a saved folder: unchecking removes it
    folder: Path | None = None  # a found folder: checking adds it


def folder_rows(root: Path, workspaces) -> list[FolderRow]:
    """Every saved folder (checked), then the found ones not saved yet (unchecked).

    With nothing saved, the recommended folder (else the first found) starts
    checked. A found folder whose repo-holding subfolders are all saved
    already is left out: their own rows show instead.
    """
    saved = _readable(workspaces)
    rows = [FolderRow(folder_label(w), "", True, workspace=w) for w in saved]
    menu = folder_menu(root, workspaces, groups=True)
    first = menu.recommended or 0
    for index, (folder, note) in enumerate(zip(menu.folders, menu.notes)):
        if folder not in menu.readable and not _groups_saved(folder, workspaces):
            rows.append(FolderRow(_display_path(folder), note.strip("()"), not saved and index == first,
                                  folder=folder))
    return rows


def _groups_saved(folder: Path, workspaces) -> bool:
    """Whether ``folder`` holds no repo itself and every subfolder of it that holds repos is a saved work folder."""
    if _holds_repos(folder):
        return False
    groups = repo_groups(folder)
    return bool(groups) and all(_cli().work_workspace(workspaces, group) is not None for group in groups)


def _check_folders(root: Path, ui, workspaces) -> None:
    """The setup window's folder step: the checked set is what tasks may work in."""
    rows = folder_rows(root, workspaces)
    saved = any(row.workspace is not None for row in rows)
    for _attempt in range(_MAX_ATTEMPTS):
        answer = ui.check_folders([(row.label, row.note) for row in rows], [row.checked for row in rows])
        if answer is None:
            ui.say("Kept the current folders." if saved else f"No folder added. {_next(ui, FOLDER_NEXT)}")
            return
        if answer:
            apply_folders(root, ui, rows, answer)
            return
        ui.say("Keep at least one folder." if saved else "Check a folder, or Skip.")
    ui.say("Kept the current folders." if saved else f"No folder added. {_next(ui, FOLDER_NEXT)}")


def apply_folders(root: Path, ui, rows: list[FolderRow], answer) -> None:
    """Make the checked set the allowed folders and say each folder's result.

    ``answer`` holds the checked row indexes and the paths added with Add
    Folder…. Adds run before removes, so the last folder is never removed
    first; a checked repo inside an unchecked folder (of repos, or one tasks
    only read) is added after that folder is removed, since until then the
    folder allows it already or blocks it.
    """
    picked = {item for item in answer if isinstance(item, int)}
    adds = [rows[i].folder for i in sorted(picked) if rows[i].workspace is None]
    adds += [item for item in answer if isinstance(item, str)]
    removes = [row.workspace for i, row in enumerate(rows) if row.workspace is not None and i not in picked]

    def within(folder, workspace) -> bool:
        return any(pathid.inside(folder, source) for source in _folder_of(workspace))

    after = [folder for folder in adds if any(within(folder, w) for w in removes)]
    # A folder of org folders whose only org is unchecked here adds nothing: the removal alone shows.
    adds = [folder for folder in adds
            if not (len(repo_groups(Path(folder))) == 1 and _groups_saved(Path(folder), removes))]
    _add_folders(root, ui, [folder for folder in adds if folder not in after])
    last = None  # the only saved folder, kept: its line waits on the adds inside it
    for workspace in removes:
        refused = _remove(root, workspace.workspace_id)
        shown = folder_label(workspace)
        if refused is None:
            ui.say(f"{printer.MARK_OK} Removed {shown} (its files are kept).")
        elif refused[0] == "last_workspace":
            last = (workspace, f"{printer.MARK_BAD} {shown}: {refused[1]}")
        else:
            ui.say(f"{printer.MARK_BAD} {shown}: {refused[1]}")
    switched = []

    def switch(folder, code) -> bool:
        # Neither refusal alone says what to do: the kept folder blocks the repo, and is the last one.
        if last is None or code != "work_overlaps_readable" or not within(folder, last[0]):
            return False
        ui.say(f"To switch to {_display_path(Path(folder))}, add another folder first, "
               f"then remove {folder_label(last[0])}.")
        switched.append(folder)
        return True

    _add_folders(root, ui, after, refused=switch)
    if last is not None and not switched:
        ui.say(last[1])


def _write_failure(exc: Exception) -> str:
    """The window's and Settings' line for a folder change that failed without a refusal code."""
    if isinstance(exc, ClaudeSwitchError) and str(exc) == "worker_lifecycle_busy":
        return _cli().BUSY_MESSAGE
    return workspace_message("settings_unavailable", gui=True)


def _remove(root: Path, workspace_id: str) -> tuple[str | None, str] | None:
    """Remove a saved folder with `worker workspace remove`'s function: None, or (code, the window's reason)."""
    cli = _cli()
    try:
        cli.remove_worker_workspace(root, workspace_id)
    except cli.WorkspaceError as exc:
        return exc.code, workspace_message(exc.code, gui=True)
    except Exception as exc:
        return None, _write_failure(exc)
    return None


def remove_folder(root: Path, workspace_id: str) -> str | None:
    """Remove a saved folder (Settings → Remote); the window's reason, marked ⚠, if refused."""
    refused = _remove(root, workspace_id)
    return None if refused is None else f"{printer.MARK_WARN} {refused[1]}"


def add_folder(root: Path, folder) -> str | None:
    """Add a folder with `worker workspace add --work`'s function (Settings → Remote).

    A folder that is no repo and holds none, but whose subfolders hold
    repos, adds each of those instead (``add_repo_groups``). Returns what to
    tell the owner (a refusal marked ⚠), or None when it was simply added.
    """
    cli = _cli()
    try:
        result = cli.add_work_folder(root, folder)
    except cli.WorkspaceError as exc:
        groups = add_repo_groups(root, folder) if exc.code == "work_not_a_repo" else None
        if groups is not None:
            text, refused, _ok = groups
            return f"{printer.MARK_WARN} {text}" if refused else text
        shown = _display_path(Path(folder))
        return f"{printer.MARK_WARN} {shown}: {_clause(workspace_message(exc.code, gui=True))}."
    except Exception as exc:
        return f"{printer.MARK_WARN} {_write_failure(exc)}"
    if result.added:
        return f'"{cli.DEFAULT_RESEARCH_ID}" stays until its running task ends.' if result.kept_builtin else None
    added = result.workspace.work_root
    parent = next((w for w in load_worker_settings(root).workspaces
                   if getattr(w, "repos", False) and w.work_root is not None
                   and not pathid.same(w.work_root, added) and pathid.inside(added, w.work_root)), None)
    return f"Already allowed through {_display_path(parent.work_root)}." if parent else "Already allowed."


UNPAIR_TITLE = "Unpair this Mac?"
UNPAIR_KEY_LEFT = ("Unpaired, but the old key couldn't be removed from the Keychain. "
                   "Connecting to the same server again replaces it.")


def unpair_message(server: str) -> str:
    return (f"Remote tasks turn off. This Mac stays listed, offline, under the old account until you remove "
            f"it on {server}.\n\nTo link it again, choose Connect to OpenTag….")


def task_blocks_unpair(status: dict) -> bool:
    """A task the running worker still has could not report its result once the Mac
    is unpaired. A stopped worker's leftover task is closed by its next start."""
    return status.get("process_state") == "running" and status.get("active_job") is not None


def unpair_this_mac(root: Path, url: str, worker_id: str | None) -> str | None:
    """Settings → Remote's Unpair…: `openswap worker unpair`'s ``pairing.unpair`` of the
    pairing the owner confirmed (``url``, ``worker_id``), refused once another replaced it
    and while a task runs (``task_blocks_unpair``). Returns a control code
    (see ``control_message``), or None once this Mac is unpaired; ``key_left``
    when it is unpaired but its old key stays in the Keychain."""
    from openswap.worker.pairing import unpair
    from openswap.worker.protocol import ProtocolError

    cli = _cli()
    try:
        cli._migrate_legacy_before_worker_state_change(root)
        # The runtime starts a task under the lifecycle lock: holding it across the
        # check and the unpair, a task either shows here or sees the cleared pairing.
        with cli.lifecycle_lock(root):
            current = load_worker_settings(root)
            if current.control_service_url is None:
                return None
            if (current.control_service_url, current.control_service_worker_id) != (url, worker_id):
                return "pairing_changed"
            try:
                status = cli.read_status(root)
            except Exception:
                return "worker_state_unavailable"
            if status.get("process_state") == "unavailable":
                return "worker_state_unavailable"
            if task_blocks_unpair(status):
                return "job_running"
            unpair(root)
    except ClaudeSwitchError as exc:
        if str(exc) in {"worker_lifecycle_busy", "worker_state_unavailable"}:
            return str(exc)
        return "settings_unavailable"
    except ProtocolError as exc:
        # device_key_unavailable is raised only after the configured URL was cleared: the
        # Mac is unpaired, but the key stays until a pairing with the same server replaces it.
        return "key_left" if exc.code == "device_key_unavailable" else exc.code
    except Exception:
        return "settings_unavailable"
    return None


def add_repo_groups(root: Path, folder) -> tuple[str, bool, bool] | None:
    """Add each subfolder of ``folder`` that holds repos as a folder of repos, each on its own.

    The setup window and Settings only, for a folder ``add_work_folder``
    refused as holding no repos (``~/GitHub`` whose repos sit in org
    folders). A refusal never undoes the subfolders added before it, and one
    saved already is skipped. None when no subfolder holds repos; else the
    line to show, whether any subfolder was refused, and whether any was not
    (added, or saved already).
    """
    cli = _cli()
    groups = repo_groups(Path(folder))
    if not groups:
        return None
    added, saved, kept_builtin, refused = 0, 0, False, []
    for group in groups:
        try:
            result = cli.add_work_folder(root, group)
        except cli.WorkspaceError as exc:
            refused.append((group, workspace_message(exc.code, gui=True)))
            continue
        except Exception as exc:
            refused.append((group, _write_failure(exc)))
            continue
        added += result.added
        saved += not result.added
        kept_builtin = kept_builtin or result.kept_builtin
    text = repo_groups_message(Path(folder), added, saved, refused)
    if kept_builtin:
        text += f' "{cli.DEFAULT_RESEARCH_ID}" stays until its running task ends.'
    return text, bool(refused), len(refused) < len(groups)


REPO_GROUPS_NAMED = 3
# Each name, path and reason is cut to these lengths, so the line fits Settings' notice bound whole.
_NAME_LIMIT, _PATH_LIMIT, _REASON_LIMIT = 60, 200, 200


def _trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def repo_groups_message(folder: Path, added: int, saved: int, refused: list[tuple[Path, str]]) -> str:
    """What adding ``folder``'s repo-holding subfolders did.

    ``added`` and ``saved`` (already allowed) count subfolders; ``refused``
    is ``(subfolder, reason)`` pairs, the first ``REPO_GROUPS_NAMED`` named.
    """
    shown = _trim(_display_path(folder), _PATH_LIMIT)
    if not refused:
        return f"Added {added} folder{'s' if added != 1 else ''} from {shown}." if added else "Already allowed."
    if not added and not saved and len(refused) == 1:
        group, reason = refused[0]
        return f"{_trim(_display_path(group), _PATH_LIMIT)}: {_trim(_clause(reason), _REASON_LIMIT)}."
    if added:
        head = f"Added {added} folder{'s' if added != 1 else ''} from {shown}"
        head += f"; {saved} already allowed" if saved else ""
    elif saved:
        head = f"{saved} already allowed from {shown}"
    else:
        head = f"No folders added from {shown}"
    names = [f"{_trim(group.name, _NAME_LIMIT)}: {_trim(_clause(reason), _REASON_LIMIT)}."
             for group, reason in refused[:REPO_GROUPS_NAMED]]
    more = len(refused) - REPO_GROUPS_NAMED
    if more > 0:
        names.append(f"And {more} more.")
    return f"{head}. {len(refused)} not added. {' '.join(names)}"


def _bad_choice(count: int) -> str:
    return f"Type numbers from 1 to {count}, or a folder path." if count else "Type a folder path."


def _search_folders(root: Path, ui, has_current: bool) -> None:
    """The terminal's folder step: the type-to-search picker, again after each pick.

    The detected folders are its numbered suggestions (GitHub first and
    highlighted, so Enter picks it); typing searches them and the home
    folder, and digits pick by number. Every pick goes through
    ``add_readable_folder`` like any other answer. Enter on nothing (or Esc)
    finishes.
    """
    from openswap.folder_picker import Suggestion

    added = False
    question = "Folders"
    for _round in range(_MAX_PICKS):
        menu = folder_menu(root, load_worker_settings(root).workspaces)
        pinned = [Suggestion(folder, note, folder in menu.readable)
                  for folder, note in zip(menu.folders, menu.notes)]
        # The default is highlighted only on the first pick, and only when
        # nothing is set up yet: then Enter takes it.
        highlight = None if added or has_current or not menu.folders else (menu.recommended or 0)
        answer = ui.search_folders(question, pinned=pinned, highlight=highlight)
        if not answer:
            if not added:
                ui.say("Kept the current folders." if has_current else f"No folder added. {FOLDER_NEXT}")
            return
        choice = parse_folder_choice(answer, len(menu.folders))
        if choice is None:
            ui.say(_bad_choice(len(menu.folders)))
            continue
        picked = [menu.folders[index] for index in choice] if isinstance(choice, list) else [choice]
        if _add_folders(root, ui, picked):
            added = True
            question = "Add another (Enter to finish)"


def _add_folders(root: Path, ui, folders, *, refused=None) -> bool:
    """Add each folder as one where sessions work and say what happened; whether any was.

    ``refused(folder, code)`` may say a refusal's line itself (True) instead.
    """
    cli = _cli()
    any_ok = False
    for folder in folders:
        shown = _display_path(Path(folder)) if os.path.isabs(str(folder)) else str(folder)
        try:
            result = cli.add_work_folder(root, folder)
        except cli.WorkspaceError as exc:
            groups = add_repo_groups(root, folder) if _gui(ui) and exc.code == "work_not_a_repo" else None
            if groups is not None:
                text, partly_refused, ok = groups
                ui.say(f"{printer.MARK_BAD if partly_refused else printer.MARK_OK} {text}")
                any_ok = any_ok or ok
            elif refused is None or not refused(folder, exc.code):
                ui.say(f"{shown}: {workspace_message(exc.code, gui=_gui(ui))}")
            continue
        except Exception as exc:
            ui.say(f"{shown}: {_write_failure(exc) if _gui(ui) else workspace_message('settings_unavailable')}")
            continue
        any_ok = True
        workspace = result.workspace
        where = _display_path(workspace.work_root)
        if workspace.repos:
            # A folder of repos: tasks name each repo in it.
            ui.say(f"{printer.MARK_OK} {where}: {', '.join(result.repos) or 'no repos yet'}")
        elif not result.added:
            ui.say(f"{printer.MARK_OK} {where} ({workspace.workspace_id}, already added)")
        else:
            ui.say(f"{printer.MARK_OK} {where} ({workspace.workspace_id})")
            _offer_direct(root, ui, workspace)
        if result.kept_builtin:
            ui.say(f"\"{cli.DEFAULT_RESEARCH_ID}\" stays until its running task ends.")
    return any_ok


def _offer_direct(root: Path, ui, workspace) -> None:
    """The advanced choice (``openswap worker setup --advanced`` only): work in the folder itself."""
    if not getattr(ui, "advanced", False) or workspace.mode == "direct":
        return
    if ui.confirm(f"Work in {_display_path(workspace.work_root)} itself, without a copy?", default=False):
        try:
            _cli().set_workspace_mode(root, workspace.workspace_id, "direct")
        except Exception:
            ui.say(f"Could not change it. Next: `openswap worker workspace mode {workspace.workspace_id} direct`.")
            return
        ui.say(f"{printer.MARK_OK} {workspace.workspace_id} works in the folder itself.")


@dataclass(frozen=True)
class Readiness:
    """What the summary reports; every field is local and path-free."""

    paired_url: str | None
    worker: str  # "running", "starting", "stopped" or "off"
    account: str | None
    folders: tuple[str, ...]
    execution: str
    # The approved workspaces that read a folder of this Mac ("id (label)").
    readable: tuple[str, ...] = ()
    # (workspace ID, code) for each workspace whose jobs the worker refuses at launch.
    refused: tuple[tuple[str, str], ...] = ()
    paused: bool = False
    # The pinned account's provider ("codex" or "claude"; None when nothing is pinned).
    provider: str | None = None  # admission paused: the worker claims no new task
    # The running worker's link to the control service: "online", "offline",
    # "revoked", "expired" or "disabled"; None when unknown.
    connection: str | None = None
    # The per-Mac limit on remote sessions ("follow", the default: each
    # account's own Claude or Codex settings; "no-shell"; "read-only").
    permission_override: str = "follow"

    @property
    def missing(self) -> tuple[str, ...]:
        """What is still missing, each as the command that fixes it."""
        return self._missing(gui=False)

    @property
    def settings_missing(self) -> tuple[str, ...]:
        """The same, each naming the Settings → Remote control that fixes it (the setup window)."""
        return self._missing(gui=True)

    def _missing(self, *, gui: bool) -> tuple[str, ...]:
        out = []
        wait = ("wait a moment, then check Status in Settings → Remote" if gui
                else "wait a moment, then `openswap worker status`")
        if self.paired_url is None:
            out.append("Connect to OpenTag… in Settings → Remote to pair this Mac" if gui
                       else "`openswap worker pair <url> <code>` to pair this Mac")
        if self.worker == "starting":
            out.append(wait)
        elif self.worker == "stopped" and gui:
            out.append(WORKER_RESTART)
        elif self.worker != "running":
            out.append("turn on Local worker in Settings → Remote" if gui
                       else "`openswap worker enable` to start the worker")
        elif self.paired_url is not None and self.connection != "online":
            # Only an online worker can receive tasks from the service.
            if gui:
                out.append({
                    "revoked": "this Mac was removed in OpenTag: Unpair… in Settings → Remote, then Connect again",
                    "expired": "Reconnect this Mac in Settings → Remote (the pairing expired)",
                }.get(self.connection, wait))
            else:
                out.append({
                    "revoked": "`openswap worker pair <url> <code>` to pair again (this Mac was removed)",
                    "expired": "`openswap worker pair <url> <code>` to pair again (the pairing expired)",
                }.get(self.connection, wait))
        # A paused worker is what the live check needs: resuming is a step only once live
        # tasks are on (until then the summary names the check, without a pause first).
        # The window has no live check, so there a pause is always a step.
        if self.paused and (self.execution == "live" or gui):
            out.append("turn off Pause new tasks in Settings → Remote to resume" if gui
                       else "`openswap worker pause --off` to resume")
        if self.account is None:
            out.append("pick an account under Account in Settings → Remote" if gui
                       else "`openswap worker account <slot>` to pick an account")
        if not self.folders:
            out.append("add a folder under Folders in Settings → Remote" if gui
                       else "`openswap worker workspace add --work <folder>` to add a folder")
        if self.refused:
            names = ", ".join(f'"{workspace_id}"' for workspace_id, _code in self.refused)
            out.append(f"remove {names} under Folders in Settings → Remote" if gui
                       else f"`openswap worker workspace list` to fix {names}")
        return tuple(out)


def _refused(root: Path, workspaces) -> tuple[tuple[str, str], ...]:
    try:
        return tuple(_cli().refused_workspaces(root, workspaces))
    except Exception:
        return ()


def readiness(root: Path) -> Readiness:
    from openswap.worker.adapter import execution_mode, pinned_adapter

    cli = _cli()
    policy = load_worker_settings(root)
    worker = "off"
    connection = None
    if policy.enabled is True:
        try:
            status = cli.read_status(root)
            process = status.get("process_state")
            connection = status.get("remote_connectivity")
        except Exception:
            process = None
        if process == "running":
            worker = "running"
        elif process == "starting" or (process in {"stopped", None} and cli._managed_worker_loaded()):
            # Just after `enable` the LaunchAgent is loaded but the process may
            # not have written its first status yet. Starting is never ready:
            # a crashed worker can stay loaded, so only `running` counts.
            worker = "starting"
        else:
            # Stale, stopping or unreadable health is reported as not running.
            worker = "stopped"
    account = None
    provider = None
    try:
        choices = cli.worker_account_choices(root)
        # Only a pinned default counts: a task without a per-task choice
        # needs it, and the allowed accounts reach the service only once it
        # acknowledges them, which the local settings cannot show.
        if choices.pinned is not None and not choices.pinned_missing:
            provider = getattr(choices.pinned, "provider", "codex")
            account = f"{_provider_name(choices.pinned)} {choices.pinned.label()}"
    except Exception:
        pass
    return Readiness(
        paired_url=policy.control_service_url,
        worker=worker,
        account=account,
        folders=tuple(_describe(w) for w in policy.workspaces),
        readable=tuple(_describe(w) for w in _picked(root, policy.workspaces)),
        refused=_refused(root, None),
        # The live mode of the pinned account's provider: a Claude pin with a
        # passing Claude live check is live, whatever the Codex opt-in says.
        execution=execution_mode(pinned_adapter(root)),
        provider=provider,
        paused=policy.paused is True,
        connection=connection if isinstance(connection, str) else None,
        permission_override=cli.permission_override(root),
    )


def settling(state: Readiness) -> bool:
    """A just-enabled worker that has not yet started or reached the service."""
    return state.worker == "starting" or (
        state.worker == "running" and state.paired_url is not None
        and state.connection in {None, "offline", "disabled"}
    )


SIGN_IN_NOTE = ("Note: a shell command in a Claude task can read or replace that account's own sign-in (macOS "
                "can't run Claude Code's sandbox inside OpenSwap's). `openswap worker permissions no-shell` "
                "prevents it.")
PERMISSIONS_OFFER = ("Tasks follow each account's own Claude or Codex permission settings. "
                     "Limit every task on this Mac? (follow, no-shell, read-only)")


def offer_permissions(root: Path, ui: Prompts) -> None:
    """The advanced choice (``openswap worker setup --advanced`` only): a per-Mac limit."""
    if not getattr(ui, "advanced", False) or not ui.interactive:
        return
    cli = _cli()
    current = cli.permission_override(root)
    answer = ui.ask(PERMISSIONS_OFFER, default=current)
    if answer is None or answer.strip() in ("", current):
        return
    value = answer.strip().lower()
    if value not in ("follow", "no-shell", "read-only"):
        ui.say(f"Kept {current}. Next: `openswap worker permissions follow|no-shell|read-only`.")
        return
    try:
        cli.set_permission_override(root, value)
    except Exception:
        ui.say(f"Could not change it. Next: `openswap worker permissions {value}`.")
        return
    ui.say(f"{printer.MARK_OK} {cli.permissions_text(value)}")


def summary(root: Path, ui: Prompts, *, start_wait_s: float | None = None) -> None:
    offer_permissions(root, ui)
    state = readiness(root)
    # Give a worker that `enable` just started a moment to report running and
    # reach the service. A front end may set ``settle_wait_s`` to change it.
    if start_wait_s is None:
        start_wait_s = getattr(ui, "settle_wait_s", 5.0)
    deadline = time.monotonic() + start_wait_s
    while settling(state) and time.monotonic() < deadline:
        time.sleep(0.25)
        state = readiness(root)
    done = getattr(ui, "done", None)
    if done is not None:
        done(state)
        return
    for line in checklist(state):
        ui.say(line)
    for workspace_id, code in state.refused:
        ui.say(_cli().refusal_line(workspace_id, code))
    if state.provider == "claude" and state.permission_override == "follow":
        # Shell is the account's to allow, and nothing can keep its own
        # sign-in from a shell command (see the live check's sign_in_isolation).
        ui.say(SIGN_IN_NOTE)
    # The checklist shows every gap; one `Next:` names the first to close.
    if state.missing:
        ui.say(f"Next: {state.missing[0]}.")
    elif state.execution != "live":
        # Nothing missing means the worker runs and takes tasks: the check needs it paused.
        ui.say(execution_off_note(state.provider, pause_first=state.worker == "running" and not state.paused))
    else:
        ui.say(READY_NOTE)


def checklist(state: Readiness) -> list[str]:
    """The readiness report as aligned ``✓``/``✗``/``•`` rows, each with its words."""
    worker = {"running": "running", "starting": "starting", "stopped": "enabled but not running",
              "off": "off"}[state.worker]
    if state.paused:
        worker += " (paused)"
    online = state.worker == "running" and state.connection
    service = f"{state.paired_url} ({state.connection})" if state.paired_url and online else (
        state.paired_url or "no")
    service_ok = None if state.paired_url is None else (
        True if not online or state.connection == "online"
        else False if state.connection in {"revoked", "expired"} else None)
    worker_ok = (True if state.worker == "running" and not state.paused
                 else None if state.worker == "starting" else False)
    rows = [
        (f"{printer.mark(service_ok)} Paired", service),
        (f"{printer.mark(worker_ok)} Worker", worker),
        (f"{printer.mark(state.account is not None)} Account", state.account or "none"),
        # No folder is allowed (tasks still run), so it is neutral, not missing.
        (f"{printer.mark(False if state.refused else True if state.readable else None)} Folders",
         ", ".join(state.readable) or "none"),
        # Live tasks stay off until the live check passes; the Next line says how.
        (f"{printer.mark(True if state.execution == 'live' else None)} Live tasks",
         "on" if state.execution == "live" else "off"),
    ]
    if state.permission_override != "follow":
        # Shown only when this Mac limits sessions beyond the accounts' own settings.
        rows.append((f"{printer.mark(None)} Permissions", f"{state.permission_override} (this Mac)"))
    return printer.columns(rows)


# Steps run in order (looked up by name), each with its header and the line
# shown if it fails unexpectedly (the setup window's for the summary: ``SUMMARY_FAILED``).
STEPS = (
    ("offer_worker", "Step 1 of 4 · Worker", START_WORKER_NEXT),
    ("confirm_account", "Step 2 of 4 · Account", ACCOUNT_NEXT),
    ("choose_folders", "Step 3 of 4 · Folders", FOLDER_NEXT),
    ("summary", "Step 4 of 4 · Summary", None),
)
SUMMARY_FAILED = f"{printer.MARK_BAD} Couldn't check what is set up. See Status in Settings → Remote."


def run(root: Path, ui: Prompts) -> None:
    """Every step after pairing; a failing step prints its manual command and the rest still run."""
    for name, title, fallback in STEPS:
        _section(ui, title)
        try:
            globals()[name](Path(root), ui)
        except Exception:
            if fallback is not None:
                ui.say(_next(ui, fallback))
            elif _gui(ui):
                ui.say(SUMMARY_FAILED)


class SetupClosed(BaseException):
    """The owner pressed Close Setup: stop every step that is left.

    A ``BaseException``, so the steps' own ``except Exception`` (which print
    a fallback command and carry on) never swallow it.
    """


# The menu bar dialogs: Skip (or No) passes one step, Close Setup ends them all.
CLOSE_SETUP = "Close Setup"
# What a dialog returns for Close Setup (``menubar_dialog.DIALOG_CLOSE``).
DIALOG_CLOSED = -3


class DialogPrompts:
    """The menu bar's pairing dialogs: ``alert``/``prompt`` are its modal dialog helpers.

    Lines said between questions are collected and shown above the next
    question; ``flush`` shows what is left in a final dialog. Every question
    also offers Close Setup, which raises :class:`SetupClosed`; nothing is
    shown after it. The steps after pairing show in the setup window
    (``openswap.worker.setup_window``).
    """

    interactive = True
    closed = False

    def __init__(self, alert, prompt, title: str = "Set up Remote tasks", *, wait=None):
        self._alert, self._prompt, self.title = alert, prompt, title
        # ``wait(title=, message=, done=)`` shows one dialog with Cancel until the
        # ``done`` event is set; it returns True when the owner cancelled.
        self._wait = wait
        self._lines: list[str] = []

    def say(self, text: str) -> None:
        self._lines.append(text)

    def section(self, title: str) -> None:
        self.say(title)

    def _message(self, question: str) -> str:
        message = "\n".join([*self._lines, question]).strip()
        self._lines = []
        return message

    def _closing(self, clicked) -> None:
        if clicked == DIALOG_CLOSED:
            self.closed = True
            self._lines = []
            raise SetupClosed()

    def ask(self, question: str, *, default: str = "") -> str | None:
        response = self._prompt(title=self.title, message=self._message(question),
                                default_text=default, ok="Continue", cancel="Skip", close=CLOSE_SETUP)
        self._closing(getattr(response, "clicked", 0))
        if getattr(response, "clicked", 0) != 1:
            return None
        return str(getattr(response, "text", "")).strip() or default

    def flush(self) -> None:
        if self._lines and not self.closed:
            self._alert(title=self.title, message=self._message(""), ok="Done")

    def waiting(self, title: str, message: str, *, cancel: Callable[[], None], done: threading.Event) -> None:
        """One dialog with Cancel until ``done`` is set; Cancel calls ``cancel``."""
        if done.is_set():
            return
        if self._wait is None:
            # No dismissable dialog on this front end: nothing to show while waiting.
            return
        if self._wait(title=title, message=self._message(message), done=done) and not done.is_set():
            cancel()


PAIRING_QUESTION = (
    "Paste the pairing command from OpenTag (Workers, Pair a Mac). "
    "It looks like: openswap worker pair https://opentag.me CODE"
)


class ThreadedPrompts:
    """Run the setup on a worker thread while dialogs stay on the UI thread.

    The worker thread calls ``ask``/``flush`` as usual; each call
    is handed to the UI thread, which shows it from ``serve()`` (the menu
    bar's timer) and wakes the worker with the answer. Lines said in between
    are only collected, so pairing, ``enable``, pins and folder changes (which
    may wait on launchctl, the network or locks) never block the UI thread.
    """

    interactive = True

    def __init__(self, dialogs: DialogPrompts):
        self._dialogs = dialogs
        self._lock = threading.Lock()
        self._pending: dict | None = None
        self.finished = False

    def say(self, text: str) -> None:
        with self._lock:
            self._dialogs.say(text)

    def section(self, title: str) -> None:
        with self._lock:
            self._dialogs.section(title)

    def _call(self, name: str, *args, **kwargs):
        request = {"call": (name, args, kwargs), "done": threading.Event(), "result": None}
        with self._lock:
            self._pending = request
        request["done"].wait()
        if request.get("closed"):
            raise SetupClosed()
        return request["result"]

    def ask(self, question: str, *, default: str = "") -> str | None:
        return self._call("ask", question, default=default)

    def flush(self) -> None:
        self._call("flush")

    def waiting(self, title: str, message: str, *, cancel: Callable[[], None]) -> Callable[[], None]:
        """Show one dialog with Cancel while the worker thread keeps working.

        Unlike the other prompts this returns at once. Cancel in the dialog calls
        ``cancel`` (from the UI thread). The returned ``close()`` dismisses the
        dialog and returns once it is gone, so the next prompt never opens on
        top of it.
        """
        done, gone = threading.Event(), threading.Event()
        request = {"call": ("waiting", (title, message), {"cancel": cancel, "done": done}),
                   "done": gone, "result": None}
        with self._lock:
            self._pending = request

        def close() -> None:
            done.set()
            gone.wait()

        return close

    def serve(self) -> None:
        """On the UI thread: show the dialog the worker thread is waiting on, if any."""
        with self._lock:
            request, self._pending = self._pending, None
        if request is None:
            return
        name, args, kwargs = request["call"]
        try:
            request["result"] = getattr(self._dialogs, name)(*args, **kwargs)
        except SetupClosed:
            # Raised again on the setup thread, which waits in ``_call``.
            request["closed"] = True
        except Exception:
            request["result"] = None
        finally:
            request["done"].set()


def ask_pairing_command(ui: Prompts) -> tuple[str, str] | None:
    """Ask for the pasted pairing command until it parses; ``None`` when skipped."""
    for _attempt in range(_MAX_ATTEMPTS):
        text = ui.ask(PAIRING_QUESTION)
        if not text:
            return None
        parsed = parse_pairing_command(text)
        if parsed is not None:
            return parsed
        ui.say("That is not a pairing command. Copy the whole line from the Workers page.")
    return None


def pair_once(root: Path, url: str, code: str) -> tuple[str, str]:
    """Pair with a parsed command; no prompts, so it can run off a UI thread.

    Returns ``(outcome, message)``: ``"paired"``, ``"retry"`` (the service
    refused the code; ask for a new one) or ``"failed"`` (stop).
    """
    from openswap.worker.pairing import pair
    from openswap.worker.protocol import ProtocolError

    try:
        _cli()._migrate_legacy_before_worker_state_change(root)
        worker_id = pair(root, url, code)
    except ProtocolError as exc:
        return "retry", f"Could not pair ({exc.code}). Codes work once for 10 minutes: make a new one."
    except (ClaudeSwitchError, OSError, RuntimeError, ValueError):
        return "failed", "Could not pair: settings unavailable."
    return "paired", f"{printer.MARK_OK} Paired this Mac ({worker_id})."


def pair_interactively(root: Path, ui: Prompts) -> bool:
    """Ask for the pairing command and pair, for front ends without a command line.

    The owner pasting the code here is the local approval, exactly like
    running ``openswap worker pair``. Returns whether this Mac is now paired.
    The menu bar instead runs ``pair_once`` on a worker thread.
    """
    for _attempt in range(_MAX_ATTEMPTS):
        parsed = ask_pairing_command(ui)
        if parsed is None:
            return False
        outcome, message = pair_once(root, *parsed)
        ui.say(message)
        if outcome != "retry":
            return outcome == "paired"
    return False


CONNECT_TITLE = "Connect to OpenTag"
CONNECT_WAITING = "Finish in your browser…"


def connect_waiting_message(reconnect: bool) -> str:
    return ("Your browser opened OpenTag. Sign in if asked, pick the workspace and select "
            f"{'Reconnect' if reconnect else 'Pair this Mac'}. OpenSwap waits up to 10 minutes.")


def connect_once(flow, ui) -> tuple[str, str]:
    """Run a Connect flow (``openswap.worker.connect.ConnectFlow``) off the UI thread.

    While the browser is open the owner sees one dialog, "Finish in your
    browser…", whose Cancel closes the listener. Returns ``(outcome, message)``:
    ``"paired"`` (a new pairing: run the guided setup next), ``"reconnected"``
    or ``"failed"``.
    """
    from openswap.worker import connect
    from openswap.worker.protocol import ProtocolError

    try:
        _cli()._migrate_legacy_before_worker_state_change(flow.root)
        flow.start()
    except connect.AlreadyPaired as exc:
        return "failed", f"This Mac is already paired with {exc.url}."
    except ProtocolError as exc:
        return "failed", _connect_failure(exc.code, flow)
    except (ClaudeSwitchError, OSError, RuntimeError, ValueError):
        flow.close()
        return "failed", "Could not connect: settings unavailable."
    close = ui.waiting(CONNECT_WAITING, connect_waiting_message(flow.target.reconnect), cancel=flow.cancel)
    try:
        result = flow.finish()
    except ProtocolError as exc:
        return "failed", _connect_failure(exc.code, flow)
    except (ClaudeSwitchError, OSError, RuntimeError, ValueError):
        return "failed", "Could not connect: settings unavailable."
    finally:
        close()
    if result.reconnected:
        return "reconnected", f"{printer.MARK_OK} Reconnected this Mac ({result.worker_id}). It keeps its jobs."
    where = result.workspace_label or result.url
    return "paired", f"{printer.MARK_OK} Paired with {where} ({result.worker_id})."


def _connect_failure(code: str, flow) -> str:
    from openswap.worker import connect

    target = getattr(flow, "target", None)
    step = connect.next_step(code, target.url if target is not None else None)
    return step if code in {"connect_cancelled", "access_denied"} else f"Could not connect ({code}). {step}"


def parse_pairing_command(text: str) -> tuple[str, str] | None:
    """``(url, code)`` from a pasted ``openswap worker pair <url> <code>`` or ``<url> <code>``."""
    try:
        words = shlex.split(text.strip())
    except ValueError:
        return None
    if words[:3] == ["openswap", "worker", "pair"]:
        words = words[3:]
    if len(words) != 2 or not words[0].lower().startswith(("https://", "http://")):
        return None
    return words[0], words[1]
