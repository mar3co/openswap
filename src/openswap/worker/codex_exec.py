"""The live Codex research adapter: pinned ``codex exec --json`` under containment.

One job, on the account the runtime leased for it:

- **Binary.** Only the hash-pinned official Codex CLI 0.157.1
  (:mod:`openswap.worker.codex_cli`), re-hashed before every launch.
- **Account.** Each eligible account gets its own isolated ``CODEX_HOME`` under
  OpenSwap's private worker directory, signed in once by the owner with
  ``openswap worker codex login`` (Codex's own login flow, file-backed
  credentials). The owner's default ``~/.codex`` login and OpenSwap's roster
  snapshots are never read, copied or written by a job; only that Codex
  process refreshes the isolated home's tokens. Before launch the isolated
  home's signed-in account ID must hash to the job's leased identity, or the
  job fails ``provider_auth_unavailable`` without launching.
- **Sandbox.** The home's ``config.toml`` (owned by OpenSwap and rewritten for
  every launch) selects a named permission profile: deny ``:root``, read only
  ``:minimal`` plus the job's approved read-only sources, write only the job's
  output directory, deny ``$TMPDIR`` and ``/tmp``. No ``--sandbox`` flag is
  ever passed, because that makes Codex ignore ``default_permissions``. Shell
  commands have no network; research uses Codex's live web search. Apps, hooks,
  plugins, multi-agent, browser/computer use, code mode, unified exec and
  skill search are disabled; project config discovery and AGENTS.md are off.
- **Process.** The job runs as its own launchd job
  (:mod:`openswap.worker.containment`) with an allowlisted environment, so the
  worker's own environment never reaches it. Stop, completion and recovery all
  end in a coalition sweep, and ``execution_stopped`` is reported only from that
  sweep's proof.

Events are reduced to the journal's allowlist: ``provider_started`` when
Codex reports its thread, and one ``provider_finished`` with the outcome. Model
text, commands and URLs stay in the job's private run directory and the
approved output folder (``result.md``); they never enter the journal.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from collections import OrderedDict
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from openswap.codex.auth import parse_auth
from openswap.worker import codex_cli
from openswap.worker.adapter import ProviderLaunchRefused
from openswap.worker.containment import (
    STDERR_FILE,
    STDOUT_FILE,
    ContainmentError,
    JobHandle,
    LaunchdContainment,
    ensure_private_dir,
    write_private,
)
from openswap.worker.leases import LeaseStateError, stable_account_identity
from openswap.worker.live import LIVE, LiveModeError, execution_mode, live_lock
from openswap.worker.models import (
    InterruptResult,
    JobRecord,
    JobState,
    ProviderAvailability,
    ProviderRun,
    ResolvedWorkspace,
    SafeEvent,
    SafeEventKind,
)
from openswap.settings import load_live_execution

PROFILE_NAME = "openswap-research"
RESULT_FILE = "result.md"
# Codex writes its final message here, in the private run directory: never in
# the model-writable folder, where a planted symlink could redirect that
# unsandboxed write. The adapter publishes it as result.md after the sweep.
LAST_MESSAGE_FILE = "last-message.md"
MAX_RESULT_BYTES = 1024 * 1024
SUMMARY_FILE = "summary.json"
MAX_LINE_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 256 * 1024
MAX_AUTH_BYTES = 1024 * 1024
EVENT_WAIT_SECONDS = 0.25
FINISHED_RUNS_KEPT = 32

# Global feature switches, exactly the set the credential-free 0.157.1
# tool-surface probe accepted under --strict-config, minus shell_tool: the
# research profile keeps the sandboxed shell so the model can read approved
# sources.
DISABLED_FEATURES = (
    "apps", "hooks", "plugins", "multi_agent", "browser_use", "browser_use_external",
    "browser_use_full_cdp_access", "computer_use", "code_mode_host", "code_mode",
    "unified_exec", "unified_exec_tty", "skill_search", "skill_mcp_dependency_install",
    "remote_plugin", "workspace_dependencies",
)

PROMPT_PREAMBLE = """You are running a bounded research task for the owner of this Mac, started remotely through OpenSwap Remote tasks.

Rules:
- Your current working directory is the only place you may write. Keep any notes or files there.
- Read-only source folders you may consult: {sources}.
- Use web search for anything current, and cite every source with its full URL.
- Do not install software, change settings, start background services, or try to reach other files or the network from the shell.
- End with your final answer in Markdown: a short summary, the findings, and a "Sources" section listing the URLs you used.

Task:
"""


def homes_root(backup_root: Path) -> Path:
    return Path(backup_root) / "worker" / "codex-homes"


def runs_root(backup_root: Path) -> Path:
    return Path(backup_root) / "worker" / "runs"


def isolated_home(backup_root: Path, identity: str) -> Path:
    """The account's own ``CODEX_HOME``, named by its opaque identity."""
    if not isinstance(identity, str) or not identity.startswith("codex:") or len(identity) != 70:
        raise ValueError("a codex: account identity is required")
    return homes_root(backup_root) / identity.split(":", 1)[1][:32]


def _toml_string(value: str) -> str:
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("control characters are not allowed in config paths")
    return json.dumps(value, ensure_ascii=False)


def codex_config(readonly_sources: tuple[Path, ...] = ()) -> str:
    """The isolated home's whole ``config.toml``; nothing else is configured."""
    lines = [
        "# Managed by OpenSwap Remote tasks and rewritten before every run. Do not edit.",
        'cli_auth_credentials_store = "file"',
        'forced_login_method = "chatgpt"',
        'approval_policy = "never"',
        f'default_permissions = "{PROFILE_NAME}"',
        "project_root_markers = []",
        "project_doc_max_bytes = 0",
        'web_search = "live"',
        "check_for_update_on_startup = false",
        'file_opener = "none"',
        "",
        "[history]",
        'persistence = "none"',
        "",
        "[analytics]",
        "enabled = false",
        "",
        "[otel]",
        'exporter = "none"',
        "",
        "[agents]",
        "enabled = false",
        "",
        "[shell_environment_policy]",
        'inherit = "core"',
        "ignore_default_excludes = false",
        "",
        f"[permissions.{PROFILE_NAME}]",
        'extends = ":workspace"',
        "",
        f"[permissions.{PROFILE_NAME}.filesystem]",
        '":root" = "deny"',
        '":minimal" = "read"',
        '":tmpdir" = "deny"',
        '":slash_tmp" = "deny"',
    ]
    for source in readonly_sources:
        lines.append(f'{_toml_string(str(Path(source)))} = "read"')
    lines += [
        "",
        f'[permissions.{PROFILE_NAME}.filesystem.":workspace_roots"]',
        '"." = "write"',
        "",
        f"[permissions.{PROFILE_NAME}.network]",
        "enabled = false",
        "",
    ]
    return "\n".join(lines)


def prepare_home(backup_root: Path, identity: str, readonly_sources: tuple[Path, ...] = ()) -> Path:
    """Create the isolated home (0700) and write its managed config."""
    root = Path(backup_root)
    ensure_private_dir(root / "worker")
    ensure_private_dir(homes_root(root))
    home = isolated_home(root, identity)
    ensure_private_dir(home)
    ensure_private_dir(home / "home")
    write_private(home / "config.toml", codex_config(readonly_sources).encode("utf-8"))
    return home


def home_identity(home: Path) -> str | None:
    """The stable identity of the ChatGPT account signed in to ``home``, if any.

    Reads only the isolated home's own ``auth.json`` (never the default login),
    and only its account ID; nothing is returned or logged from it.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(Path(home) / "auth.json", flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AUTH_BYTES:
            return None
        text = os.read(fd, MAX_AUTH_BYTES).decode("utf-8", "replace")
    finally:
        os.close(fd)
    identity = parse_auth(text)
    if identity is None or identity.kind != "oauth" or not identity.account_id:
        return None
    try:
        return stable_account_identity("codex", identity.account_id)
    except LeaseStateError:
        return None


def codex_env(home: Path, run_dir: Path) -> dict[str, str]:
    """The job's whole environment: an allowlist, never the worker's own."""
    try:
        import pwd

        user = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        user = str(os.getuid())
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home / "home"),
        "CODEX_HOME": str(home),
        "TMPDIR": str(run_dir / "tmp"),
        "LANG": "en_US.UTF-8",
        "USER": user,
        "LOGNAME": user,
        "SHELL": "/bin/zsh",
    }


def global_args() -> list[str]:
    args = ["--strict-config"]
    for feature in DISABLED_FEATURES:
        args += ["--disable", feature]
    return args


def codex_argv(binary: Path, output_root: Path, run_dir: Path) -> list[str]:
    """Fixed launcher arguments; nothing comes from the remote caller."""
    return [
        str(binary), *global_args(), "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "--cd", str(output_root), "--output-last-message", str(run_dir / LAST_MESSAGE_FILE), "-",
    ]


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def publish_result(run_dir: Path, output_root: Path) -> bool:
    """Copy Codex's final message to ``output_root/result.md`` without following links.

    Runs only after the job's processes are proven gone, so nothing can race
    it. Whatever the model left at that name is removed first (a directory
    there fails the job); the new file is created exclusively, never through
    a symlink.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(run_dir / LAST_MESSAGE_FILE, flags)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_size > MAX_RESULT_BYTES:
            return False
        data = os.read(fd, MAX_RESULT_BYTES)
    finally:
        os.close(fd)
    target = output_root / RESULT_FILE
    try:
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                return False
            os.unlink(target)
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return False
    try:
        os.write(out, data)
    finally:
        os.close(out)
    return True


def build_prompt(task: str, workspace: ResolvedWorkspace) -> str:
    sources = ", ".join(str(path) for path in workspace.readonly_sources) or "none"
    return PROMPT_PREAMBLE.format(sources=sources) + task + "\n"


def classify_failure(message: str) -> str:
    text = message.lower()
    if any(word in text for word in ("rate limit", "rate_limit", "usage limit", "429", "quota", "too many requests")):
        return "provider_rate_limited"
    if any(word in text for word in (
        "401", "unauthorized", "not logged in", "log in", "login", "refresh token", "token expired",
        "authentication",
    )):
        return "provider_auth_unavailable"
    return "provider_unavailable"


@dataclass
class _Run:
    job_id: str
    handle: JobHandle
    run_dir: Path
    output_root: Path
    lock: threading.Lock = field(default_factory=threading.Lock)
    offset: int = 0
    pending: bytes = b""
    skipping_long_line: bool = False
    cursor: int = 0
    started: bool = False
    finished: bool = False
    turn_completed: bool = False
    failure: str | None = None
    item_counts: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    unparsed_lines: int = 0


class CodexExecAdapter:
    """Production adapter; runs nothing unless live mode is on (see :mod:`live`)."""

    def __init__(
        self,
        backup_root: Path,
        *,
        containment: LaunchdContainment | None = None,
        verify=None,
        mode=None,
        monotonic=time.monotonic,
        sleep=time.sleep,
        bind_to_opt_in: bool = True,
    ):
        self.backup_root = Path(backup_root)
        # The live check runs before (or to renew) the opt-in, so it checks
        # the binary it verified itself rather than the one an older opt-in
        # recorded.
        self._bind_to_opt_in = bind_to_opt_in
        self._containment = containment
        self._verify = verify or (lambda **kw: codex_cli.verify(self.backup_root, **kw))
        self._mode = mode or (lambda: execution_mode(self.backup_root))
        self._monotonic = monotonic
        self._sleep = sleep
        self._runs: dict[int, _Run] = {}
        # Stop proof of runs that already finished, so a late interrupt()
        # still reports it; bounded, oldest dropped first.
        self._finished: OrderedDict[int, bool] = OrderedDict()
        self._runs_lock = threading.Lock()

    @property
    def execution_mode(self) -> str:
        """``"live"`` once the owner opted in (re-read on every access), else ``"disabled"``."""
        try:
            return LIVE if self._mode() == LIVE else "disabled"
        except Exception:
            return "disabled"

    @property
    def containment(self) -> LaunchdContainment:
        if self._containment is None:
            self._containment = LaunchdContainment()
        return self._containment

    # -- probe / start ----------------------------------------------------------

    def _pinned(self, *, check_version: bool):
        pinned = self._verify(check_version=check_version)
        if not self._bind_to_opt_in:
            return pinned
        expected = load_live_execution(self.backup_root).codex_sha256
        if expected is not None and pinned.binary_sha256 != expected:
            raise codex_cli.CodexCliError("binary_not_the_checked_one")
        return pinned

    def probe(self) -> ProviderAvailability:
        if self._mode() != LIVE:
            return ProviderAvailability(False, "live_adapter_disabled", None)
        try:
            pinned = self._pinned(check_version=True)
        except codex_cli.CodexCliError:
            return ProviderAvailability(False, "provider_unavailable", None)
        return ProviderAvailability(True, None, pinned.version)

    def start(self, job: JobRecord, workspace: ResolvedWorkspace, *, worker_epoch: int) -> ProviderRun:
        # The opt-in check and the provider's release are one step under the
        # live lock, so a `live disable` that returned cannot be overtaken.
        try:
            with live_lock(self.backup_root):
                return self._start_locked(job, workspace, worker_epoch=worker_epoch)
        except (LiveModeError, ContainmentError):
            raise ProviderLaunchRefused("provider_unavailable") from None

    def _start_locked(self, job: JobRecord, workspace: ResolvedWorkspace, *, worker_epoch: int) -> ProviderRun:
        if self._mode() != LIVE:
            raise ProviderLaunchRefused("live_adapter_disabled")
        identity = job.pinned_account_ref
        if not isinstance(identity, str):
            raise ProviderLaunchRefused("provider_auth_unavailable")
        try:
            pinned = self._pinned(check_version=False)
        except codex_cli.CodexCliError:
            raise ProviderLaunchRefused("provider_unavailable") from None
        try:
            home = prepare_home(self.backup_root, identity, tuple(workspace.readonly_sources))
        except (OSError, ValueError, ContainmentError):
            raise ProviderLaunchRefused("provider_unavailable") from None
        if home_identity(home) != identity:
            # Not signed in, or signed in to a different account than the
            # leased one: never run on whatever is there.
            raise ProviderLaunchRefused("provider_auth_unavailable")
        output_root = Path(workspace.output_root)
        private = (self.backup_root / "worker").resolve()
        granted = [output_root.resolve(), *(Path(p).resolve() for p in workspace.readonly_sources)]
        if any(_overlaps(private, path) for path in granted):
            # A writable or readable root that contains (or is inside) the
            # worker's private directory would expose CODEX_HOME to the model.
            raise ProviderLaunchRefused("provider_unavailable")
        try:
            ensure_private_dir(runs_root(self.backup_root))
            run_dir = runs_root(self.backup_root) / job.job_id
            if os.path.lexists(run_dir):
                # A run directory means this job already had a launch attempt;
                # a job is never launched twice.
                raise ProviderLaunchRefused("execution_uncertain")
            ensure_private_dir(run_dir)
            ensure_private_dir(run_dir / "tmp")
        except (OSError, ContainmentError):
            raise ProviderLaunchRefused("provider_unavailable") from None
        try:
            handle = self.containment.launch(
                job_id=job.job_id, run_dir=run_dir, argv=codex_argv(pinned.binary, output_root, run_dir),
                env=codex_env(home, run_dir), cwd=output_root,
                stdin_text=build_prompt(job.task, workspace),
            )
        except ContainmentError as error:
            if not error.launched:
                raise ProviderLaunchRefused("provider_unavailable") from None
            raise RuntimeError("execution_uncertain") from None
        run = _Run(job.job_id, handle, run_dir, output_root)
        with self._runs_lock:
            self._runs[handle.leader_pid] = run
            self._finished.pop(handle.leader_pid, None)
        return ProviderRun(
            process_id=handle.leader_pid, session_id=None, worker_epoch=worker_epoch,
            generation=job.generation, provider_event_cursor=0,
        )

    # -- events -----------------------------------------------------------------

    def _run_for(self, run: ProviderRun) -> _Run | None:
        with self._runs_lock:
            return self._runs.get(run.process_id)

    def events(self, run: ProviderRun, *, after_cursor: int) -> tuple[SafeEvent, ...]:
        state = self._run_for(run)
        if state is None:
            return ()
        deadline = self._monotonic() + EVENT_WAIT_SECONDS
        while True:
            with state.lock:
                if state.finished:
                    return ()
                events = self._read_new(state)
                if events:
                    return tuple(events)
                containment = self.containment
                exited = containment.exit_status(state.handle) is not None
                if exited or not containment.leader_alive(state.handle):
                    # Drain whatever the provider wrote before it ended.
                    events = self._read_new(state, drain=True)
                    finished = self._finish(state)
                    events.append(finished)
                    self._forget(run.process_id, finished.execution_stopped)
                    return tuple(events)
            if self._monotonic() >= deadline:
                return ()
            self._sleep(0.05)

    @staticmethod
    def _event(run: _Run, kind: SafeEventKind, **fields) -> SafeEvent:
        run.cursor += 1
        return SafeEvent(run.job_id, run.cursor, datetime.now(timezone.utc), kind, **fields)

    def _read_new(self, state: _Run, *, drain: bool = False) -> list[SafeEvent]:
        out: list[SafeEvent] = []
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(state.run_dir / STDOUT_FILE, flags)
        except OSError:
            return out
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return out
            os.lseek(fd, state.offset, os.SEEK_SET)
            while True:
                chunk = os.read(fd, READ_CHUNK_BYTES)
                if not chunk:
                    break
                state.offset += len(chunk)
                out.extend(self._consume(state, chunk))
                if not drain:
                    break
        finally:
            os.close(fd)
        if drain and state.pending and not state.skipping_long_line:
            out.extend(self._line(state, state.pending))
            state.pending = b""
        return out

    def _consume(self, state: _Run, chunk: bytes) -> list[SafeEvent]:
        out = []
        data = state.pending + chunk
        lines = data.split(b"\n")
        state.pending = lines.pop()
        for line in lines:
            if state.skipping_long_line:
                state.skipping_long_line = False
                continue
            if len(line) > MAX_LINE_BYTES:
                state.unparsed_lines += 1
                continue
            out.extend(self._line(state, line))
        if len(state.pending) > MAX_LINE_BYTES:
            state.pending = b""
            state.skipping_long_line = True
            state.unparsed_lines += 1
        return out

    def _line(self, state: _Run, line: bytes) -> list[SafeEvent]:
        if not line.strip():
            return []
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            state.unparsed_lines += 1
            return []
        if not isinstance(record, dict):
            state.unparsed_lines += 1
            return []
        kind = record.get("type")
        if kind == "thread.started" and not state.started:
            state.started = True
            return [self._event(state, SafeEventKind.PROVIDER_STARTED)]
        if kind == "turn.completed":
            state.turn_completed = True
            usage = record.get("usage")
            if isinstance(usage, dict):
                state.usage = {k: v for k, v in usage.items() if isinstance(k, str) and type(v) is int}
        elif kind == "turn.failed":
            error = record.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            state.failure = classify_failure(message if isinstance(message, str) else "")
        elif kind == "error":
            message = record.get("message")
            state.failure = state.failure or classify_failure(message if isinstance(message, str) else "")
        elif isinstance(kind, str) and kind.startswith("item."):
            item = record.get("item")
            item_type = item.get("type") if isinstance(item, dict) else None
            if isinstance(item_type, str) and len(item_type) <= 64 and kind == "item.completed":
                state.item_counts[item_type] = state.item_counts.get(item_type, 0) + 1
        return []

    def _result_ok(self, state: _Run) -> bool:
        try:
            info = (state.run_dir / LAST_MESSAGE_FILE).lstat()
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_RESULT_BYTES

    def _finish(self, state: _Run) -> SafeEvent:
        """Sweep the job, then report its outcome with the sweep's proof."""
        exit_status = self.containment.exit_status(state.handle)
        proof = self.containment.stop(state.handle)
        state.finished = True
        if (exit_status == 0 and state.turn_completed and state.failure is None and self._result_ok(state)
                and proof.stopped is True and publish_result(state.run_dir, state.output_root)):
            outcome, diagnostic = JobState.SUCCEEDED, None
        else:
            outcome = JobState.FAILED
            diagnostic = state.failure or self._stderr_failure(state)
        self._write_summary(state, exit_status, proof.to_dict(), outcome.value, diagnostic)
        return self._event(
            state, SafeEventKind.PROVIDER_FINISHED, state=outcome, diagnostic_code=diagnostic,
            execution_stopped=proof.stopped is True,
        )

    def _stderr_failure(self, state: _Run) -> str:
        try:
            with open(state.run_dir / STDERR_FILE, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 16384))
                tail = handle.read().decode("utf-8", "replace")
        except OSError:
            return "provider_unavailable"
        return classify_failure(tail)

    def _write_summary(self, state: _Run, exit_status, proof: dict, outcome: str, diagnostic) -> None:
        """Counts only (no model text): kept beside the run for the owner and the live check."""
        summary = {
            "job_id": state.job_id, "exit_status": exit_status, "outcome": outcome,
            "diagnostic_code": diagnostic, "thread_started": state.started,
            "turn_completed": state.turn_completed, "item_counts": state.item_counts,
            "usage": state.usage, "unparsed_lines": state.unparsed_lines, "stop_proof": proof,
            "result_present": self._result_ok(state),
        }
        try:
            write_private(state.run_dir / SUMMARY_FILE, json.dumps(summary).encode())
        except OSError:
            pass

    def _forget(self, process_id: int, stopped: bool) -> None:
        with self._runs_lock:
            self._runs.pop(process_id, None)
            self._finished[process_id] = stopped is True
            while len(self._finished) > FINISHED_RUNS_KEPT:
                self._finished.popitem(last=False)

    # -- interrupt / recover ----------------------------------------------------

    def interrupt(self, run: ProviderRun) -> InterruptResult:
        state = self._run_for(run)
        if state is None:
            with self._runs_lock:
                stopped = self._finished.get(run.process_id)
            if stopped is not None:
                return InterruptResult(requested=False, execution_stopped=stopped,
                                       diagnostic_code=None if stopped else "execution_uncertain")
            return InterruptResult(requested=False, execution_stopped=False, diagnostic_code="execution_uncertain")
        with state.lock:
            if state.finished:
                summary = self._read_summary(state)
                stopped = isinstance(summary, dict) and summary.get("stop_proof", {}).get("stopped") is True
            else:
                proof = self.containment.stop(state.handle)
                state.finished = True
                stopped = proof.stopped is True
                self._write_summary(state, self.containment.exit_status(state.handle), proof.to_dict(),
                                    "interrupted", None)
        self._forget(run.process_id, stopped)
        return InterruptResult(
            requested=True, execution_stopped=stopped,
            diagnostic_code=None if stopped else "execution_uncertain",
        )

    def _read_summary(self, state: _Run):
        try:
            return json.loads((state.run_dir / SUMMARY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def recover(self, job_id: str) -> InterruptResult | None:
        """Stop whatever a lost worker left running for ``job_id``.

        ``None`` when this adapter never launched it (no run handle). Otherwise
        the job's launchd label is unloaded and its coalition swept; the result
        carries proof only when nothing of it is left.
        """
        run_dir = runs_root(self.backup_root) / job_id
        if not os.path.isdir(run_dir) or os.path.islink(run_dir):
            return None
        try:
            proof = self.containment.recover(run_dir)
        except ContainmentError:
            return InterruptResult(requested=True, execution_stopped=False, diagnostic_code="execution_uncertain")
        if proof is None:
            return None
        return InterruptResult(
            requested=True, execution_stopped=proof.stopped is True,
            diagnostic_code=None if proof.stopped else "execution_uncertain",
        )

