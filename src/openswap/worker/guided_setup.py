"""The guided Remote tasks setup shared by ``openswap worker pair`` and the menu bar.

After pairing, the owner is walked through the same steps everywhere: start the
worker, confirm the Codex or Claude account, approve a research folder, then a summary
of what is still missing before Slack can start tasks on this Mac. Each step
uses the same functions as the matching ``openswap worker`` command; the
front end only supplies prompts (``Prompts``). Pairing has already succeeded
when these run, and nothing here can undo or fail it.
"""

from __future__ import annotations

import re
import shlex
import threading
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

    def choose_folder(self, question: str) -> str | None:
        """A folder path; "" or ``None`` when the owner is done choosing."""


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

    def choose_folder(self, question: str) -> str | None:
        if self.read_line is not None:
            return self.ask(question)
        from openswap.folder_picker import pick_folder

        return pick_folder(question)


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
    "Next: approve a research folder: `openswap worker workspace add <id> <folder>` "
    "(or run `openswap worker setup` again to approve ~/OpenSwap Research). Only the ID "
    "and a label reach the control service; the folder path never leaves this Mac."
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
    """Show the pinned account (Codex or Claude) and offer to keep it, or pick one."""
    cli = _cli()
    try:
        choices = cli.worker_account_choices(root)
    except Exception:
        choices = None
    pinned = choices.pinned if choices is not None and not choices.pinned_missing else None
    if pinned is not None:
        ui.say(f"Remote tasks uses {_provider_name(pinned)} account {pinned.label()}.")
        if not ui.interactive or ui.confirm("Keep this account?") is not False:
            return
    elif not ui.interactive or choices is None:
        ui.say(ACCOUNT_NEXT)
        return
    elif choices.pinned_missing:
        ui.say("The pinned account is no longer in the roster; choose another.")
    eligible = [choice for choice in choices.codex if choice.eligible]
    eligible_claude = [choice for choice in choices.claude if choice.eligible]
    if not eligible and not eligible_claude:
        ui.say("No eligible account is saved. Add one with `openswap codex add` or `openswap add`, then "
               "`openswap worker account <slot>`.")
        return
    # Skipping after declining the current pin cancels the change: say the
    # previous account stays selected rather than implying none is pinned.
    later = (f"{pinned.label()} stays selected. Change it later with "
             "`openswap worker account <slot|email|alias>`." if pinned is not None
             else "Pin one later with `openswap worker account <slot|email|alias>`.")
    ui.say("Choose the account remote jobs run on:")
    for choice in eligible:
        ui.say(f"  {choice.label()}  (Codex)")
    for choice in eligible_claude:
        ui.say(f"  claude:{choice.label()}  (Claude)")
    for _attempt in range(_MAX_ATTEMPTS):
        answer = ui.ask("Account (slot, email or alias; claude:<slot> for Claude; Enter to skip): ")
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
        ui.say(f"Pinned {_provider_name(chosen)} account {chosen.label()} for Remote tasks.")
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
        answer = ui.choose_folder("Another folder to approve (path; Enter to finish): ")
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
            out.append("approve a research folder (`openswap worker workspace add <id> <folder>`)")
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
    ui.say(EXECUTION_LIVE_NOTE if state.execution == "live" else execution_off_note(state.provider))


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

    def __init__(self, alert, prompt, title: str = "Set up Remote tasks", *, choose_folder=None):
        self._alert, self._prompt, self.title = alert, prompt, title
        # ``choose_folder(title=, message=)`` opens the native folder chooser and
        # returns a path or None; without one, folders are typed into a prompt.
        self._choose_folder = choose_folder
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

    def choose_folder(self, question: str) -> str | None:
        if self._choose_folder is None:
            return self.ask(question)
        return self._choose_folder(title=self.title, message=self._message(question))

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
