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

    def choose_folder(self, question: str) -> str | None:
        """A folder path; "" or ``None`` when the owner is done choosing."""


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
    # One home-folder scan shared by every folder search in a run.
    _index: object = field(default=None, repr=False)

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


def execution_off_note(provider: str | None) -> str:
    """The one `Next:` line for turning on live tasks with the pinned account's provider."""
    return CLAUDE_EXECUTION_OFF_NOTE if provider == "claude" else EXECUTION_OFF_NOTE


def _provider_name(choice) -> str:
    return "Claude" if getattr(choice, "provider", "codex") == "claude" else "Codex"


WORKER_OFFER = "Start the worker now?"
WORKER_ONLINE = f"{printer.MARK_OK} Worker started. This Mac shows online in about 15 seconds."
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
    if load_worker_settings(root).enabled is True:
        try:
            process = cli.read_status(root).get("process_state")
        except Exception:
            process = None
        if process in _RUNNING_STATES:
            ui.say(f"{printer.MARK_OK} Worker running.")
        else:
            ui.say("The worker is on but not running. Next: `openswap worker enable`.")
        return
    if not ui.interactive:
        ui.say(START_WORKER_NEXT)
        return
    if ui.confirm(WORKER_OFFER) is not True:
        ui.say(f"Not started. {START_WORKER_NEXT}")
        return
    try:
        cli.enable_worker(root)
    except ClaudeSwitchError as exc:
        code = str(exc)
        message = cli._enable_failure_message(exc)
        if code in cli._ENABLE_DIAGNOSTICS:
            message = f"{message.rstrip('.')} ({code})."
        ui.say(f"{message} {START_WORKER_NEXT}")
    except Exception:
        ui.say(f"Could not enable worker. {START_WORKER_NEXT}")
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
        ui.say(f"{printer.MARK_OK} Account: {_provider_name(pinned)} {pinned.label()}")
        return
    if not ui.interactive or choices is None:
        ui.say(ACCOUNT_NEXT)
        return
    if choices.pinned_missing:
        ui.say("Your pinned account is gone; pick another.")
    menu, lines = account_menu(choices, pinned)
    if not menu:
        ui.say("No account to pick. Next: `openswap codex add` or `openswap add`.")
        return
    # Skipping after declining the current pin cancels the change: say the
    # previous account stays selected rather than implying none is pinned.
    later = (f"Kept {_provider_name(pinned)} {pinned.label()}." if pinned is not None
             else f"Skipped. {ACCOUNT_NEXT}")
    ui.say("Tasks run on the account you pick.")
    for line in lines:
        ui.say(line)
    current = next((str(n) for n, c in enumerate(menu, start=1)
                    if pinned is not None and c.account_ref == pinned.account_ref), "")
    # A number, an email, an alias or `claude:<slot>` all work.
    question = "Account" if current else f"Account (1-{len(menu)}, Enter skips)"
    for _attempt in range(_MAX_ATTEMPTS):
        answer = ui.ask(question, default=current)
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
            ui.say(cli._ACCOUNT_MESSAGES.get(exc.code, f"Could not pin that account ({exc.code})."))
            continue
        except Exception:
            ui.say(f"Could not pick that account. {ACCOUNT_NEXT}")
            return
        ui.say(f"{printer.MARK_OK} {_provider_name(chosen)} {chosen.label()}")
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
    notes: tuple[str, ...] = ()  # per folder: "recommended", "git repo", "added as <id>"


def folder_menu(root: Path, workspaces) -> FolderMenu:
    """Number the detected folders (a GitHub folder first, "recommended"); ✓ marks readable ones."""
    cli = _cli()
    try:
        detected = cli.detect_code_folders(root)
    except Exception:
        detected = []
    # Repos and folders of repos first: a session needs one to work in.
    detected = [folder for folder in detected if _has_repos(folder)] + \
        [folder for folder in detected if not _has_repos(folder)]
    reads = {}
    for workspace in _readable(workspaces):
        for source in _folder_of(workspace):
            reads.setdefault(Path(source), workspace.workspace_id)
    folders = list(detected) + [source for source in reads if source not in detected]
    recommended = 0 if detected and cli.is_github_folder(detected[0]) else None
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


def _add_folders(root: Path, ui, folders) -> bool:
    """Add each folder as one where sessions work and say what happened; whether any was."""
    cli = _cli()
    any_ok = False
    for folder in folders:
        shown = _display_path(Path(folder)) if os.path.isabs(str(folder)) else str(folder)
        try:
            result = cli.add_work_folder(root, folder)
        except cli.WorkspaceError as exc:
            ui.say(f"{shown}: {_workspace_message(exc.code)}")
            continue
        except Exception:
            ui.say(f"{shown}: {_workspace_message('settings_unavailable')}")
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
        out = []
        if self.paired_url is None:
            out.append("`openswap worker pair <url> <code>` to pair this Mac")
        if self.worker == "starting":
            out.append("wait a moment, then `openswap worker status`")
        elif self.worker != "running":
            out.append("`openswap worker enable` to start the worker")
        elif self.paired_url is not None and self.connection != "online":
            # Only an online worker can receive tasks from the service.
            out.append({
                "revoked": "`openswap worker pair <url> <code>` to pair again (this Mac was removed)",
                "expired": "`openswap worker pair <url> <code>` to pair again (the pairing expired)",
            }.get(self.connection, "wait a moment, then `openswap worker status`"))
        if self.paused:
            out.append("`openswap worker pause --off` to resume")
        if self.account is None:
            out.append("`openswap worker account <slot>` to pick an account")
        if not self.folders:
            out.append("`openswap worker workspace add --read <folder>` to add a folder")
        if self.refused:
            names = ", ".join(f'"{workspace_id}"' for workspace_id, _code in self.refused)
            out.append(f"`openswap worker workspace list` to fix {names}")
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


def _settling(state: Readiness) -> bool:
    """A just-enabled worker that has not yet started or reached the service."""
    return state.worker == "starting" or (
        state.worker == "running" and state.paired_url is not None
        and state.connection in {None, "offline", "disabled"}
    )


SIGN_IN_NOTE = ("Note: shell commands in remote Claude tasks can read or replace that account's own sign-in (macOS "
                "cannot run Claude Code's own sandbox inside OpenSwap's). `openswap worker permissions "
                "no-shell` prevents it.")
PERMISSIONS_OFFER = ("Remote sessions follow each account's own Claude or Codex permission settings. "
                     "Limit every remote task on this Mac? (follow, no-shell, read-only)")


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
    while _settling(state) and time.monotonic() < deadline:
        time.sleep(0.25)
        state = readiness(root)
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
        ui.say(execution_off_note(state.provider))
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

    def __init__(self, alert, prompt, title: str = "Set up Remote tasks", *, choose_folder=None):
        self._alert, self._prompt, self.title = alert, prompt, title
        # ``choose_folder(title=, message=)`` opens the native folder chooser and
        # returns a path or None; without one, folders are typed into a prompt.
        self._choose_folder = choose_folder
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

    def choose_folder(self, question: str) -> str | None:
        if self._choose_folder is None:
            return self.ask(question)
        return self._choose_folder(title=self.title, message=self._message(question))

    def flush(self) -> None:
        if self._lines:
            self._alert(title=self.title, message=self._message(""), ok="Done")


PAIRING_QUESTION = (
    "Paste the pairing command from OpenTag (Workers, Pair a Mac). "
    "It looks like: openswap worker pair https://opentag.me CODE"
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

    def choose_folder(self, question: str) -> str | None:
        return self._call("choose_folder", question)

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
