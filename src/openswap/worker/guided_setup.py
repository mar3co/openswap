"""The guided Remote tasks setup shared by ``openswap worker pair`` and the menu bar.

After pairing, the owner is walked through the same steps everywhere: start the
worker, confirm the Codex account, approve a research folder, then a summary
of what is still missing before Slack can start tasks on this Mac. Each step
uses the same functions as the matching ``openswap worker`` command; the
front end only supplies prompts (``Prompts``). Pairing has already succeeded
when these run, and nothing here can undo or fail it.
"""

from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from openswap.exceptions import ClaudeSwitchError
from openswap.settings import load_worker_settings


class Prompts(Protocol):
    """What a front end supplies. ``interactive`` False means print next steps only."""

    interactive: bool

    def say(self, text: str) -> None: ...

    def confirm(self, question: str, *, default: bool = True) -> bool | None:
        """Yes/no; ``None`` when the owner gave no answer (EOF, closed dialog)."""

    def ask(self, question: str, *, default: str = "") -> str | None:
        """Free text, stripped; ``None`` when the owner gave no answer."""


@dataclass
class TerminalPrompts:
    """``input()``/``print`` prompts for the CLI; ``read_line`` is injectable for tests."""

    interactive: bool
    read_line: Callable[[str], str] | None = None
    write: Callable[[str], None] = field(default=print)

    def say(self, text: str) -> None:
        self.write(text)

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
        answer = self._read(f"{question} [{default}] " if default else question)
        if answer is None:
            return None
        return answer.strip() or default


# Copy shared by the steps and their fallbacks.
START_WORKER_NEXT = (
    "Next: start the Remote tasks worker so this Mac can accept approved tasks: "
    "`openswap worker enable`."
)
ACCOUNT_NEXT = (
    "Next: pin the Codex account remote jobs run on: `openswap worker account <slot|email|alias>` "
    "(`openswap worker account` lists them)."
)
FOLDER_NEXT = (
    "Next: approve a research folder: `openswap worker workspace add <id> <folder>` "
    "(or run `openswap worker setup` again to approve ~/OpenSwap Research). Only the ID "
    "and a label reach the control service; the folder path never leaves this Mac."
)
EXECUTION_OFF_NOTE = (
    "Task execution itself stays off until the production adapter is enabled "
    "(provider: live_adapter_disabled), so jobs are refused for now."
)
EXECUTION_LIVE_NOTE = "Task execution is live: approved tasks run on this Mac with the chosen Codex account."
WORKER_OFFER = "Start the Remote tasks worker now so this Mac can accept approved tasks?"
WORKER_ONLINE = "Remote tasks worker enabled. The portal shows this Mac online within about 15 seconds."
_RUNNING_STATES = frozenset({"starting", "running"})
_MAX_ATTEMPTS = 3
_MAX_EXTRA_FOLDERS = 8


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


def confirm_account(root: Path, ui: Prompts) -> None:
    """Show the pinned Codex account and offer to keep it, or pick one."""
    cli = _cli()
    try:
        choices = cli.worker_account_choices(root)
    except Exception:
        choices = None
    pinned = choices.pinned if choices is not None and not choices.pinned_missing else None
    if pinned is not None:
        ui.say(f"Remote tasks uses Codex account {pinned.label()}.")
        if not ui.interactive or ui.confirm("Keep this account?") is not False:
            return
    elif not ui.interactive or choices is None:
        ui.say(ACCOUNT_NEXT)
        return
    elif choices.pinned_missing:
        ui.say("The pinned Codex account is no longer in the roster; choose another.")
    eligible = [choice for choice in choices.codex if choice.eligible]
    if not eligible:
        ui.say("No eligible Codex account is saved. Add one with `openswap codex add`, then "
               "`openswap worker account <slot>`.")
        return
    # Skipping after declining the current pin cancels the change: say the
    # previous account stays selected rather than implying none is pinned.
    later = (f"{pinned.label()} stays selected. Change it later with "
             "`openswap worker account <slot|email|alias>`." if pinned is not None
             else "Pin one later with `openswap worker account <slot|email|alias>`.")
    ui.say("Choose the Codex account remote jobs run on (Claude accounts aren't supported yet):")
    for choice in eligible:
        ui.say(f"  {choice.label()}")
    for _attempt in range(_MAX_ATTEMPTS):
        answer = ui.ask("Account (slot, email or alias; Enter to skip): ")
        if not answer:
            ui.say(f"Skipped. {later}")
            return
        try:
            chosen = cli.set_worker_account(root, answer)
        except cli.AccountPinError as exc:
            ui.say(cli._ACCOUNT_MESSAGES.get(exc.code, f"Could not pin that account ({exc.code})."))
            continue
        except Exception:
            ui.say(f"Could not pin that account. {later}")
            return
        ui.say(f"Pinned Codex account {chosen.label()} for Remote tasks.")
        return
    ui.say(later)


def _display_path(path: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def _describe(workspace) -> str:
    return f"{workspace.workspace_id} ({workspace.display_label})"


def suggested_folder_id(folder: Path, taken: set[str]) -> str:
    """A free workspace ID from a folder's name: lowercase letters, digits, '-' and '_'."""
    base = re.sub(r"[^a-z0-9_-]+", "-", Path(folder).name.lower()).strip("-_")[:56] or "folder"
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def _workspace_message(code: str) -> str:
    cli = _cli()
    return cli._WORKSPACE_MESSAGES.get(code, f"Could not approve that folder ({code}).")


def approve_folders(root: Path, ui: Prompts) -> None:
    """Approve ~/OpenSwap Research as ``research`` (created owner-only), then others."""
    cli = _cli()
    policy = load_worker_settings(root)
    if not ui.interactive:
        if cli.is_builtin_default_registry(root, policy.workspaces):
            ui.say(FOLDER_NEXT)
        else:
            ui.say("Approved research folders: " + ", ".join(_describe(w) for w in policy.workspaces) + ".")
        return
    if cli.is_builtin_default_registry(root, policy.workspaces):
        folder = cli.default_research_folder()
        ui.say("Remote tasks write only inside research folders you approve. The control service "
               "sees each folder's ID and label, never its path.")
        if ui.confirm(f"Create and approve {_display_path(folder)} as research folder "
                      f"\"{cli.DEFAULT_RESEARCH_ID}\"?") is True:
            try:
                workspace = cli.add_worker_workspace(
                    root, cli.DEFAULT_RESEARCH_ID, folder, replace_builtin_default=True,
                )
            except cli.WorkspaceError as exc:
                ui.say(_workspace_message(exc.code))
            except Exception:
                ui.say(_workspace_message("settings_unavailable"))
            else:
                ui.say(f"Approved {_display_path(workspace.output_root)} as \"{workspace.workspace_id}\" "
                       f"(the portal shows \"{workspace.display_label}\").")
        else:
            ui.say(FOLDER_NEXT)
    else:
        ui.say("Approved research folders: " + ", ".join(_describe(w) for w in policy.workspaces) + ".")
    for _extra in range(_MAX_EXTRA_FOLDERS):
        answer = ui.ask("Another folder to approve (path; Enter to finish): ")
        if not answer:
            return
        try:
            folder = cli._absolute_folder(answer)
        except cli.WorkspaceError as exc:
            ui.say(_workspace_message(exc.code))
            continue
        taken = {w.workspace_id for w in load_worker_settings(root).workspaces}
        workspace_id = ui.ask("Folder ID", default=suggested_folder_id(folder, taken))
        if not workspace_id:
            continue
        try:
            workspace = cli.add_worker_workspace(root, workspace_id, folder)
        except cli.WorkspaceError as exc:
            ui.say(_workspace_message(exc.code))
            continue
        except Exception:
            ui.say(_workspace_message("settings_unavailable"))
            continue
        ui.say(f"Approved {_display_path(workspace.output_root)} as \"{workspace.workspace_id}\" "
               f"(the portal shows \"{workspace.display_label}\").")


@dataclass(frozen=True)
class Readiness:
    """What the summary reports; every field is local and path-free."""

    paired_url: str | None
    worker: str  # "running", "starting", "stopped" or "off"
    account: str | None
    folders: tuple[str, ...]
    execution: str
    paused: bool = False  # admission paused: the worker claims no new task
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
            out.append("pin a Codex account (`openswap worker account <slot>`)")
        if not self.folders:
            out.append("approve a research folder (`openswap worker workspace add <id> <folder>`)")
        return tuple(out)


def readiness(root: Path) -> Readiness:
    from openswap.worker.adapter import execution_mode

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
    try:
        choices = cli.worker_account_choices(root)
        # Only a pinned default counts: a task without a per-task choice
        # needs it, and the allowed accounts reach the service only once it
        # acknowledges them, which the local settings cannot show.
        if choices.pinned is not None and not choices.pinned_missing:
            account = choices.pinned.label()
    except Exception:
        pass
    return Readiness(
        paired_url=policy.control_service_url,
        worker=worker,
        account=account,
        folders=tuple(_describe(w) for w in policy.workspaces),
        execution=execution_mode(),
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
    # reach the service. Front ends that must not block (the menu bar's UI
    # thread) set ``settle_wait_s = 0`` and show the current state instead.
    if start_wait_s is None:
        start_wait_s = getattr(ui, "settle_wait_s", 5.0)
    deadline = time.monotonic() + start_wait_s
    while _settling(state) and time.monotonic() < deadline:
        time.sleep(0.25)
        state = readiness(root)
    worker = {"running": "running", "starting": "starting", "stopped": "enabled but not running",
              "off": "off"}[state.worker]
    ui.say("Remote tasks setup:")
    link = f" ({state.connection})" if state.paired_url and state.worker == "running" and state.connection else ""
    ui.say(f"  Service: {state.paired_url or 'not paired'}{link}")
    ui.say(f"  Worker: {worker}{' (admission paused)' if state.paused else ''}")
    ui.say(f"  Account: {state.account or 'none pinned'}")
    ui.say(f"  Research folders: {', '.join(state.folders) or 'none'}")
    ui.say(f"  Execution: {state.execution}")
    if state.missing:
        ui.say("Before Slack can start tasks on this Mac: " + "; ".join(state.missing) + ".")
    else:
        ui.say("Ready for Slack: approved tasks from your Slack workspace can reach this Mac.")
    ui.say(EXECUTION_LIVE_NOTE if state.execution == "live" else EXECUTION_OFF_NOTE)


# Steps run in order (looked up by name), each with the line shown if it fails unexpectedly.
STEPS = (
    ("offer_worker", START_WORKER_NEXT),
    ("confirm_account", ACCOUNT_NEXT),
    ("approve_folders", FOLDER_NEXT),
    ("summary", None),
)


def run(root: Path, ui: Prompts) -> None:
    """Every step after pairing; a failing step prints its manual command and the rest still run."""
    for name, fallback in STEPS:
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
    # The dialogs run on the menu bar's UI thread: never wait for the worker.
    settle_wait_s = 0.0

    def __init__(self, alert, prompt, title: str = "Set up Remote tasks"):
        self._alert, self._prompt, self.title = alert, prompt, title
        self._lines: list[str] = []

    def say(self, text: str) -> None:
        self._lines.append(text)

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


def pair_interactively(root: Path, ui: Prompts) -> bool:
    """Ask for the pairing command and pair, for front ends without a command line.

    The owner pasting the code here is the local approval, exactly like
    running ``openswap worker pair``. Returns whether this Mac is now paired.
    """
    from openswap.worker.pairing import pair
    from openswap.worker.protocol import ProtocolError

    cli = _cli()
    for _attempt in range(_MAX_ATTEMPTS):
        text = ui.ask(PAIRING_QUESTION)
        if not text:
            return False
        parsed = parse_pairing_command(text)
        if parsed is None:
            ui.say("That is not a pairing command. Copy the whole command from the Workers page.")
            continue
        try:
            cli._migrate_legacy_before_worker_state_change(root)
            worker_id = pair(root, *parsed)
        except ProtocolError as exc:
            ui.say(f"Could not pair: {exc.code}. Pairing codes are single-use and expire after "
                   "10 minutes; create a new one if needed.")
            continue
        except (ClaudeSwitchError, OSError, RuntimeError, ValueError):
            ui.say("Could not pair: local settings unavailable.")
            return False
        ui.say(f"Paired worker {worker_id}.")
        return True
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
