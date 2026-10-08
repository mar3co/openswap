"""The guided Remote tasks setup shared by ``openswap worker pair`` and the menu bar.

After pairing, the owner is walked through the same steps everywhere: start the
worker, confirm the Codex or Claude account, choose the folders tasks may read, then a
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

from openswap import printer
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


def _section(ui, title: str) -> None:
    section = getattr(ui, "section", None)
    (section or ui.say)(title)


@dataclass
class TerminalPrompts:
    """``input()``/``print`` prompts for the CLI; ``read_line`` is injectable for tests.

    Only this front end styles anything (a bold step header when colours are
    on): the step text itself stays plain, because the menu bar shows the same
    lines in dialogs.
    """

    interactive: bool
    read_line: Callable[[str], str] | None = None
    write: Callable[[str], None] = field(default=print)

    def say(self, text: str) -> None:
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


# Copy shared by the steps and their fallbacks.
START_WORKER_NEXT = (
    "Next: start the Remote tasks worker so this Mac can accept approved tasks: "
    "`openswap worker enable`."
)
ACCOUNT_NEXT = (
    "Next: pin the account remote jobs run on: `openswap worker account <slot|email|alias>` "
    "(`claude:<slot>` for a Claude account; `openswap worker account` lists them)."
)
FOLDER_NEXT = (
    "Next: choose a folder remote tasks may read, such as ~/GitHub: "
    "`openswap worker workspace add --read <folder>` (or run `openswap worker setup` again). "
    "Tasks never change it, and results are saved under ~/OpenSwap Research. Only an ID and "
    "the folder's name reach the control service; the path never leaves this Mac."
)
EXECUTION_OFF_NOTE = (
    "Task execution itself stays off (provider: live_adapter_disabled) until you run "
    "`openswap worker live-check` on this Mac and enable live execution, so jobs are refused for now."
)
CLAUDE_EXECUTION_OFF_NOTE = (
    "Task execution itself stays off (provider: live_adapter_disabled) until the Claude live check "
    "passes on this Mac, so jobs are refused for now. Run `openswap worker claude pin`, then "
    "`openswap worker claude prepare`, then `openswap worker live-check --provider claude` "
    "and enable live execution."
)
EXECUTION_LIVE_NOTE = "Task execution is live: approved tasks run on this Mac with the chosen account."


def execution_off_note(provider: str | None) -> str:
    """The off-note with the live-check steps for the pinned account's provider."""
    return CLAUDE_EXECUTION_OFF_NOTE if provider == "claude" else EXECUTION_OFF_NOTE


def _provider_name(choice) -> str:
    return "Claude" if getattr(choice, "provider", "codex") == "claude" else "Codex"
WORKER_OFFER = "Start the Remote tasks worker now so this Mac can accept approved tasks?"
WORKER_ONLINE = "Remote tasks worker enabled. The portal shows this Mac online within about 15 seconds."
_RUNNING_STATES = frozenset({"starting", "running"})
_MAX_ATTEMPTS = 3


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
    if load_worker_settings(root).enabled is True:
        try:
            process = cli.read_status(root).get("process_state")
        except Exception:
            process = None
        if process in _RUNNING_STATES:
            ui.say("The Remote tasks worker is already running on this Mac.")
        else:
            ui.say("The Remote tasks worker is enabled but not running. Run `openswap worker enable` "
                   "to start it again, or `openswap worker status` to check.")
        return
    if not ui.interactive:
        ui.say(START_WORKER_NEXT)
        return
    if ui.confirm(WORKER_OFFER) is not True:
        ui.say("Not started. Start it later with `openswap worker enable`.")
        return
    try:
        cli.enable_worker(root)
    except ClaudeSwitchError as exc:
        code = str(exc)
        message = cli._enable_failure_message(exc)
        if code in cli._ENABLE_DIAGNOSTICS:
            message = f"{message.rstrip('.')} ({code})."
        ui.say(f"{message} Start it later with `openswap worker enable`.")
    except Exception:
        ui.say("Could not enable worker. Start it later with `openswap worker enable`.")
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


def confirm_account(root: Path, ui: Prompts) -> None:
    """Show the eligible accounts as a numbered menu; Enter keeps the pinned one."""
    cli = _cli()
    try:
        choices = cli.worker_account_choices(root)
    except Exception:
        choices = None
    pinned = choices.pinned if choices is not None and not choices.pinned_missing else None
    if pinned is not None and not ui.interactive:
        ui.say(f"Remote tasks uses {_provider_name(pinned)} account {pinned.label()}.")
        return
    if not ui.interactive or choices is None:
        ui.say(ACCOUNT_NEXT)
        return
    if choices.pinned_missing:
        ui.say("The pinned account is no longer in the roster; choose another.")
    menu, lines = account_menu(choices, pinned)
    if not menu:
        ui.say("No eligible account is saved. Add one with `openswap codex add` or `openswap add`, then "
               "`openswap worker account <slot>`.")
        return
    # Skipping after declining the current pin cancels the change: say the
    # previous account stays selected rather than implying none is pinned.
    later = (f"{pinned.label()} stays selected. Change it later with "
             "`openswap worker account <slot|email|alias>`." if pinned is not None
             else "Pin one later with `openswap worker account <slot|email|alias>`.")
    ui.say("Choose the account remote jobs run on. Type its number (an email, alias or "
           "`claude:<slot>` works too).")
    for line in lines:
        ui.say(line)
    current = next((str(n) for n, c in enumerate(menu, start=1)
                    if pinned is not None and c.account_ref == pinned.account_ref), "")
    question = (f"Account number (1-{len(menu)}; Enter keeps the current one)" if current
                else f"Account number (1-{len(menu)}; Enter to skip)")
    for _attempt in range(_MAX_ATTEMPTS):
        answer = ui.ask(question, default=current)
        if not answer:
            ui.say(f"Skipped. {later}")
            return
        if answer == current:
            ui.say(f"Kept {_provider_name(pinned)} account {pinned.label()}.")
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
            ui.say(cli._ACCOUNT_MESSAGES.get(exc.code, f"Could not pin that account ({exc.code})."))
            continue
        except Exception:
            ui.say(f"Could not pin that account. {later}")
            return
        ui.say(f"Pinned {_provider_name(chosen)} account {chosen.label()} for Remote tasks.")
        return
    ui.say(later)


def _display_path(path: Path) -> str:
    path = Path(path)
    home = _cli().home_folder()
    for base in (home, _resolved(home)):
        if base is None:
            continue
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        return "~/" + relative.as_posix() if relative.parts else "~"
    return str(path)


def _resolved(path: Path) -> Path | None:
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return None


def _describe(workspace) -> str:
    return f"{workspace.workspace_id} ({workspace.display_label})"


def folder_lines(workspaces) -> list[str]:
    """One numbered, aligned line per readable folder: ID, label, then the path (local only)."""
    return printer.columns([
        (f"{printer.MARK_OK} {number}", workspace.workspace_id, f'"{workspace.display_label}"',
         ", ".join(_display_path(source) for source in workspace.readonly_roots))
        for number, workspace in enumerate(workspaces, start=1)
    ])


def _readable(workspaces) -> list:
    return [w for w in workspaces if w.readonly_roots]


def _say_folders(ui, workspaces) -> None:
    ui.say("Folders remote tasks may read, never change (the control service sees only the ID and label):")
    for line in folder_lines(workspaces):
        ui.say(line)


def suggested_folder_id(folder: Path, taken: set[str]) -> str:
    """A free workspace ID from a folder's name (see ``cli.suggested_folder_id``)."""
    return _cli().suggested_folder_id(folder, taken)


def _workspace_message(code: str) -> str:
    cli = _cli()
    return cli._WORKSPACE_MESSAGES.get(code, f"Could not approve that folder ({code}).")


@dataclass(frozen=True)
class FolderMenu:
    """The folder step's numbered list: detected code folders, then ones already readable."""

    folders: tuple[Path, ...]
    lines: tuple[str, ...]
    recommended: int | None  # menu index of the recommended GitHub folder
    readable: frozenset[Path]  # folders a workspace already reads


def folder_menu(root: Path, workspaces) -> FolderMenu:
    """Number the detected folders (a GitHub folder first, "recommended"); ✓ marks readable ones."""
    cli = _cli()
    try:
        detected = cli.detect_code_folders(root)
    except Exception:
        detected = []
    reads = {}
    for workspace in _readable(workspaces):
        for source in workspace.readonly_roots:
            reads.setdefault(Path(source), workspace.workspace_id)
    folders = list(detected) + [source for source in reads if source not in detected]
    recommended = 0 if detected and cli.is_github_folder(detected[0]) else None
    parents = set(detected)
    rows = []
    for index, folder in enumerate(folders):
        notes = []
        if index == recommended:
            notes.append("recommended")
        if folder.parent in parents and folder in detected:
            notes.append("git repo")
        if folder in reads:
            notes.append(f"readable as {reads[folder]}")
        rows.append((f"{printer.mark(True if folder in reads else None)} {index + 1}", _display_path(folder),
                     f"({', '.join(notes)})" if notes else ""))
    return FolderMenu(tuple(folders), tuple(printer.columns(rows)), recommended, frozenset(reads))


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
    if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    # Terminal escapes a dropped path's spaces with backslashes; on Windows a
    # backslash is the path separator.
    if "\\" in text and os.name != "nt":
        try:
            words = shlex.split(text)
        except ValueError:
            words = []
        if len(words) == 1:
            return words[0]
    return text


def choose_folders(root: Path, ui: Prompts) -> None:
    """Ask which folders remote tasks may read; each becomes a read-only workspace.

    Results go to ``~/OpenSwap Research/<id>``, created owner-only without
    asking. Enter takes the recommended GitHub folder (else the first one
    found), or keeps the current folders when some are readable already.
    """
    cli = _cli()
    policy = load_worker_settings(root)
    current = _readable(policy.workspaces)
    if not ui.interactive:
        if current:
            _say_folders(ui, current)
        else:
            ui.say(FOLDER_NEXT)
        return
    menu = folder_menu(root, policy.workspaces)
    ui.say("Remote tasks can read the folders you choose here, but never change them.")
    ui.say(f"Results are saved under {_display_path(cli.default_research_folder())}. The control "
           "service sees only each folder's ID and name, never its path.")
    if menu.folders:
        for line in menu.lines:
            ui.say(line)
        if current:
            default, enter = "", "Enter keeps the current ones"
        else:
            default = str((menu.recommended or 0) + 1)
            enter = f"Enter for {default}"
        question = f"Folders tasks may read (numbers like 1 3, or a path; {enter})"
    else:
        default = ""
        question = ("Type the path to your code folder, for example ~/GitHub "
                    f"(Enter {'keeps the current ones' if current else 'to skip'})")
    later = ("Change them later with `openswap worker workspace add --read <folder>`." if current
             else "Choose one later with `openswap worker workspace add --read <folder>`, or run "
                  "`openswap worker setup` again.")
    for _attempt in range(_MAX_ATTEMPTS):
        answer = ui.ask(question, default=default)
        if not answer:
            ui.say(f"Kept the current folders. {later}" if current else f"No folder chosen. {later}")
            return
        choice = parse_folder_choice(answer, len(menu.folders))
        if choice is None:
            ui.say(f"Type numbers from 1 to {len(menu.folders)} (like 1 3), or one folder path."
                   if menu.folders else "Type one folder path.")
            continue
        picked = [menu.folders[index] for index in choice] if isinstance(choice, list) else [choice]
        if _add_folders(root, ui, picked):
            return
    ui.say(later)


def _add_folders(root: Path, ui, folders) -> bool:
    """Make each folder readable and say what happened; whether any is readable now."""
    cli = _cli()
    any_ok = False
    for folder in folders:
        shown = _display_path(folder) if isinstance(folder, Path) else folder
        try:
            result = cli.add_readable_folder(root, folder)
        except cli.WorkspaceError as exc:
            ui.say(f"{shown}: {_workspace_message(exc.code)}")
            continue
        except Exception:
            ui.say(f"{shown}: {_workspace_message('settings_unavailable')}")
            continue
        any_ok = True
        workspace = result.workspace
        (source,) = workspace.readonly_roots
        if not result.added:
            ui.say(f"{printer.MARK_OK} {_display_path(source)} is already readable as \"{workspace.workspace_id}\".")
            continue
        ui.say(f"{printer.MARK_OK} Tasks can read {_display_path(source)} as \"{workspace.workspace_id}\" "
               f"(the portal shows \"{workspace.display_label}\"); results go to "
               f"{_display_path(workspace.output_root)}.")
        if result.kept_builtin:
            ui.say(f"The built-in \"{cli.DEFAULT_RESEARCH_ID}\" folder stays approved while a task still uses "
                   f"it; remove it later with `openswap worker workspace remove {cli.DEFAULT_RESEARCH_ID}`.")
    return any_ok


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
    paused: bool = False
    # The pinned account's provider ("codex" or "claude"; None when nothing is pinned).
    provider: str | None = None  # admission paused: the worker claims no new task
    # The running worker's link to the control service: "online", "offline",
    # "revoked", "expired" or "disabled"; None when unknown.
    connection: str | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        out = []
        if self.paired_url is None:
            out.append("pair this Mac (`openswap worker pair <url> <code>`)")
        if self.worker == "starting":
            out.append("wait for the worker to finish starting (`openswap worker status`)")
        elif self.worker != "running":
            out.append("start the worker (`openswap worker enable`)")
        elif self.paired_url is not None and self.connection != "online":
            # Only an online worker can receive tasks from the service.
            out.append({
                "revoked": "pair this Mac again: the service revoked it (`openswap worker pair <url> <code>`)",
                "expired": "pair this Mac again: its pairing expired (`openswap worker pair <url> <code>`)",
            }.get(self.connection, "wait for the worker to connect to the service (`openswap worker status`)"))
        if self.paused:
            out.append("reopen admission (`openswap worker pause --off`)")
        if self.account is None:
            out.append("pin an account (`openswap worker account <slot>`, or `claude:<slot>`)")
        if not self.folders:
            out.append("choose a folder tasks may read (`openswap worker workspace add --read <folder>`)")
        return tuple(out)


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
        readable=tuple(_describe(w) for w in _readable(policy.workspaces)),
        # The live mode of the pinned account's provider: a Claude pin with a
        # passing Claude live check is live, whatever the Codex opt-in says.
        execution=execution_mode(pinned_adapter(root)),
        provider=provider,
        paused=policy.paused is True,
        connection=connection if isinstance(connection, str) else None,
    )


def _settling(state: Readiness) -> bool:
    """A just-enabled worker that has not yet started or reached the service."""
    return state.worker == "starting" or (
        state.worker == "running" and state.paired_url is not None
        and state.connection in {None, "offline", "disabled"}
    )


def summary(root: Path, ui: Prompts, *, start_wait_s: float | None = None) -> None:
    state = readiness(root)
    # Give a worker that `enable` just started a moment to report running and
    # reach the service. A front end may set ``settle_wait_s`` to change it.
    if start_wait_s is None:
        start_wait_s = getattr(ui, "settle_wait_s", 5.0)
    deadline = time.monotonic() + start_wait_s
    while _settling(state) and time.monotonic() < deadline:
        time.sleep(0.25)
        state = readiness(root)
    for line in checklist(state):
        ui.say(line)
    if state.missing:
        ui.say("Before Slack can start tasks on this Mac:")
        for number, step in enumerate(state.missing, start=1):
            ui.say(f"  {number}. {step}")
    else:
        ui.say("Ready for Slack: approved tasks from your Slack workspace can reach this Mac.")
    ui.say(EXECUTION_LIVE_NOTE if state.execution == "live" else execution_off_note(state.provider))


def checklist(state: Readiness) -> list[str]:
    """The readiness report as aligned ``✓``/``✗``/``•`` rows, each with its words."""
    worker = {"running": "running", "starting": "starting", "stopped": "enabled but not running",
              "off": "off"}[state.worker]
    if state.paused:
        worker += " (admission paused)"
    online = state.worker == "running" and state.connection
    service = f"{state.paired_url} ({state.connection})" if state.paired_url and online else (
        state.paired_url or "not paired")
    service_ok = None if state.paired_url is None else (
        True if not online or state.connection == "online"
        else False if state.connection in {"revoked", "expired"} else None)
    worker_ok = (True if state.worker == "running" and not state.paused
                 else None if state.worker == "starting" else False)
    rows = [
        (f"{printer.mark(service_ok)} Service", service),
        (f"{printer.mark(worker_ok)} Worker", worker),
        (f"{printer.mark(state.account is not None)} Account", state.account or "none pinned"),
        # Reading no folder is allowed (tasks still run), so it is neutral, not missing.
        (f"{printer.mark(True if state.readable else None)} Readable folders",
         ", ".join(state.readable) or "none (tasks read no folder on this Mac)"),
        # Execution stays off until the live check passes; the note below says how.
        (f"{printer.mark(True if state.execution == 'live' else None)} Execution", state.execution),
    ]
    return printer.columns(rows)


# Steps run in order (looked up by name), each with its header and the line
# shown if it fails unexpectedly.
STEPS = (
    ("offer_worker", "Step 1 of 4 · Worker", START_WORKER_NEXT),
    ("confirm_account", "Step 2 of 4 · Account", ACCOUNT_NEXT),
    ("choose_folders", "Step 3 of 4 · Folders", FOLDER_NEXT),
    ("summary", "Step 4 of 4 · Summary", None),
)


def run(root: Path, ui: Prompts) -> None:
    """Every step after pairing; a failing step prints its manual command and the rest still run."""
    for name, title, fallback in STEPS:
        _section(ui, title)
        try:
            globals()[name](Path(root), ui)
        except Exception:
            if fallback is not None:
                ui.say(fallback)


class DialogPrompts:
    """Prompts for the menu bar: ``alert``/``prompt`` are its modal dialog helpers.

    Lines said between questions are collected and shown above the next
    question, so the owner sees one dialog per decision; ``flush`` shows what
    is left (the summary) in a final dialog.
    """

    interactive = True

    def __init__(self, alert, prompt, title: str = "Set up Remote tasks"):
        self._alert, self._prompt, self.title = alert, prompt, title
        self._lines: list[str] = []

    def say(self, text: str) -> None:
        self._lines.append(text)

    def section(self, title: str) -> None:
        self.say(title)

    def _message(self, question: str) -> str:
        message = "\n".join([*self._lines, question]).strip()
        self._lines = []
        return message

    def confirm(self, question: str, *, default: bool = True) -> bool | None:
        clicked = self._alert(title=self.title, message=self._message(question), ok="Yes", cancel="No")
        return clicked == 1

    def ask(self, question: str, *, default: str = "") -> str | None:
        response = self._prompt(title=self.title, message=self._message(question),
                                default_text=default, ok="Continue", cancel="Skip")
        if getattr(response, "clicked", 0) != 1:
            return None
        return str(getattr(response, "text", "")).strip() or default

    def flush(self) -> None:
        if self._lines:
            self._alert(title=self.title, message=self._message(""), ok="Done")


PAIRING_QUESTION = (
    "Paste the pairing command from your control service (in OpenTag: Workers page, "
    "Pair a Mac). It looks like: openswap worker pair https://opentag.me CODE"
)


class ThreadedPrompts:
    """Run the setup on a worker thread while dialogs stay on the UI thread.

    The worker thread calls ``confirm``/``ask``/``flush`` as usual; each call
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
        return request["result"]

    def confirm(self, question: str, *, default: bool = True) -> bool | None:
        return self._call("confirm", question, default=default)

    def ask(self, question: str, *, default: str = "") -> str | None:
        return self._call("ask", question, default=default)

    def flush(self) -> None:
        self._call("flush")

    def serve(self) -> None:
        """On the UI thread: show the dialog the worker thread is waiting on, if any."""
        with self._lock:
            request, self._pending = self._pending, None
        if request is None:
            return
        name, args, kwargs = request["call"]
        try:
            request["result"] = getattr(self._dialogs, name)(*args, **kwargs)
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
        ui.say("That is not a pairing command. Copy the whole command from the Workers page.")
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
        return "retry", (f"Could not pair: {exc.code}. Pairing codes are single-use and expire after "
                         "10 minutes; create a new one if needed.")
    except (ClaudeSwitchError, OSError, RuntimeError, ValueError):
        return "failed", "Could not pair: local settings unavailable."
    return "paired", f"Paired worker {worker_id}."


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
