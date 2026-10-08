"""The owner's local account pin and approved research folders for Remote tasks.

Roster files here hold metadata only; one slot also has a synthetic auth file
whose token must never be read into any output. No Keychain, launchctl or
provider auth is touched.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap.locking import FileLock
from openswap.settings import (
    WorkerSettings,
    WorkerWorkspace,
    configure_worker_local_policy,
    configure_worker_service,
    load_worker_settings,
    update_worker_settings,
)
from openswap.worker import cli, pairing
from openswap.worker.accounts import AccountPinError, codex_account_in_roster
from openswap.worker.journal import validate_event_fields
from openswap.worker.leases import AccountLeaseStore, stable_account_identity
from openswap.worker.models import JobState, SafeEventKind
from openswap.worker.runtime import WorkerRuntime
from tests.test_worker_core import _FakeAdapter, _finished_event, _submission
from tests.test_worker_pairing_status import PairTransport, keychain  # noqa: F401 (fixture)

SECRET = "synthetic-refresh-token-never-printed"
ALICE = stable_account_identity("codex", "acct-alice")
BOB = stable_account_identity("codex", "acct-bob")
CAROL = stable_account_identity("claude", "carol@example.com", "")
CLAUDE_ALICE = stable_account_identity("claude", "alice@example.com", "")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A backup root with two eligible Codex slots, an API-key slot and Claude slots."""
    backup = tmp_path / "backup"
    (backup / "codex" / "slots" / "1").mkdir(parents=True)
    os.chmod(backup, 0o700)
    _write_codex_roster(backup, {
        "1": {"email": "alice@example.com", "accountId": "acct-alice", "alias": "work", "kind": "oauth"},
        "2": {"email": "bob@example.com", "accountId": "acct-bob", "kind": "oauth", "disabled": True},
        "3": {"email": "", "accountId": "", "kind": "api_key"},
        "5": {"email": "shared@example.com", "accountId": "acct-s1", "kind": "oauth"},
        "6": {"email": "shared@example.com", "accountId": "acct-s2", "kind": "oauth"},
    })
    (backup / "codex" / "slots" / "1" / "auth.json").write_text(
        json.dumps({"tokens": {"refresh_token": SECRET}}), encoding="utf-8",
    )
    (backup / "sequence.json").write_text(json.dumps({"accounts": {
        "1": {"email": "alice@example.com"},
        "4": {"email": "carol@example.com", "alias": "claudey"},
    }}), encoding="utf-8")
    return backup


def _write_codex_roster(backup: Path, accounts: dict) -> None:
    (backup / "codex").mkdir(parents=True, exist_ok=True)
    (backup / "codex" / "sequence.json").write_text(json.dumps({
        "schemaVersion": 1, "sequence": [int(n) for n in accounts], "accounts": accounts,
    }), encoding="utf-8")


def _run(root: Path, *argv: str) -> int:
    return cli.main(list(argv), backup_root=root)


# --- openswap worker account ----------------------------------------------------


def test_account_list_marks_the_pin_and_lists_claude_accounts(root, capsys):
    assert _run(root, "account") == 0
    out = capsys.readouterr().out
    assert "1 · alice@example.com (work)" in out
    assert "3 · (no email)  [not eligible: no ChatGPT account ID (API key)]" in out
    assert "Claude (pin with `claude:<slot>`):" in out
    assert "4 · carol@example.com (claudey)" in out
    assert "No account pinned" in out
    assert SECRET not in out

    assert _run(root, "account", "2") == 0
    capsys.readouterr()
    assert _run(root, "account") == 0
    out = capsys.readouterr().out
    assert "* 2 · bob@example.com  [pinned; out of rotation]" in out


def test_account_list_json_is_metadata_only(root, capsys):
    assert _run(root, "account", "work") == 0
    capsys.readouterr()
    assert _run(root, "account", "--json") == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert SECRET not in raw
    assert payload["pinned_account_ref"] == ALICE
    assert payload["pinned_slot"] == "1" and payload["pinned_missing"] is False
    by_number = {row["number"]: row for row in payload["codex"]}
    assert by_number["1"]["pinned"] is True and by_number["1"]["account_ref"] == ALICE
    assert by_number["3"]["eligible"] is False and by_number["3"]["account_ref"] is None
    assert all(row["eligible"] is True and row["provider"] == "claude" for row in payload["claude"])
    assert {row["number"] for row in payload["claude"]} == {"1", "4"}
    assert {row["account_ref"] for row in payload["claude"]} == {CLAUDE_ALICE, CAROL}


@pytest.mark.parametrize("selector", ["1", "alice@example.com", "work", ALICE])
def test_account_pins_a_codex_slot_by_number_email_alias_or_reference(root, capsys, selector):
    assert _run(root, "account", selector) == 0
    assert "Remote tasks will use Codex account 1 · alice@example.com (work)" in capsys.readouterr().out
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_account_pin_reuses_the_codex_engine_resolution(root, monkeypatch):
    from openswap.codex.engine import CodexEngine

    calls = []
    original = CodexEngine.resolve_account

    def spy(self, identifier):
        calls.append(identifier)
        return original(self, identifier)

    monkeypatch.setattr(CodexEngine, "resolve_account", spy)
    cli.set_worker_account(root, "bob@example.com")
    assert calls == ["bob@example.com"]
    assert load_worker_settings(root).pinned_account_ref == BOB


def test_account_clear_and_json_pin(root, capsys):
    assert _run(root, "account", "2", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"accepted": True, "pinned": {
        "provider": "codex", "number": "2", "email": "bob@example.com", "alias": None, "account_ref": BOB,
    }}
    assert _run(root, "account", "--clear") == 0
    assert "Cleared" in capsys.readouterr().out
    assert load_worker_settings(root).pinned_account_ref is None


@pytest.mark.parametrize(("selector", "code"), [
    ("claude:9", "account_not_found"),
    ("nobody@example.com", "account_not_found"),
    ("9", "account_not_found"),
    ("3", "account_not_eligible"),
    ("shared@example.com", "account_ambiguous"),
    (stable_account_identity("codex", "acct-gone"), "account_not_found"),
])
def test_account_refusals_never_change_the_pin(root, capsys, selector, code):
    cli.set_worker_account(root, "1")
    assert _run(root, "account", selector, "--json") == 1
    assert json.loads(capsys.readouterr().out) == {"accepted": False, "diagnostic_code": code}
    assert _run(root, "account", selector) == 1
    assert capsys.readouterr().err.strip() == cli._ACCOUNT_MESSAGES[code]
    assert load_worker_settings(root).pinned_account_ref == ALICE


@pytest.mark.parametrize("selector", ["alice@example.com", "work"])
def test_a_malformed_roster_record_refuses_cleanly(root, capsys, selector):
    """Email and alias lookups read every roster record: one non-object record
    gives a clean refusal, never a traceback, and the pin is unchanged."""
    roster = json.loads((root / "codex" / "sequence.json").read_text(encoding="utf-8"))
    roster["accounts"]["7"] = "not an object"
    (root / "codex" / "sequence.json").write_text(json.dumps(roster), encoding="utf-8")
    before = load_worker_settings(root).pinned_account_ref
    assert _run(root, "account", selector, "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "roster_unavailable"
    assert load_worker_settings(root).pinned_account_ref == before


@pytest.mark.parametrize(("selector", "expected"), [
    ("claudey", CAROL), ("4", CAROL), ("carol@example.com", CAROL), ("claude:4", CAROL),
    ("claude:1", CLAUDE_ALICE), (CAROL, CAROL),
])
def test_a_claude_account_pins_as_a_typed_claude_reference(root, capsys, selector, expected):
    """Owner decision 2026-10-07: Claude accounts are eligible. A bare selector is
    a Codex slot first, so slot 1 needs ``claude:1``; the pin is ``claude:``-typed."""
    assert _run(root, "account", selector) == 0
    assert "Remote tasks will use Claude account" in capsys.readouterr().out
    assert load_worker_settings(root).pinned_account_ref == expected


def test_a_bare_selector_still_means_the_codex_slot_first(root):
    cli.set_worker_account(root, "1")
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_settings_store_only_typed_references(root):
    with pytest.raises(ValueError):
        configure_worker_local_policy(root, pinned_account_ref="openai:" + "0" * 64,
                                      workspaces=load_worker_settings(root).workspaces)


def test_account_selector_and_clear_together_is_a_usage_error(root, capsys):
    assert _run(root, "account", "1", "--clear") == 2
    assert load_worker_settings(root).pinned_account_ref is None


def test_pinning_holds_the_lifecycle_lock(root, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.1)
    (root / "worker").mkdir(mode=0o700, exist_ok=True)
    with FileLock(root / "worker" / "lifecycle.lock"):
        assert _run(root, "account", "1", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "worker_lifecycle_busy"
    assert load_worker_settings(root).pinned_account_ref is None


def test_pinning_holds_the_codex_mutation_guard(root, monkeypatch, capsys):
    """`codex remove/swap/move` hold this guard, so a slot cannot vanish mid-pin."""
    monkeypatch.setattr(cli, "_LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.1)
    with AccountLeaseStore(root, "codex").mutation_guard():
        assert _run(root, "account", "1", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "account_roster_busy"
    assert load_worker_settings(root).pinned_account_ref is None


def test_pin_keeps_the_workspace_registry_and_other_settings(root, tmp_path):
    folder = tmp_path / "research-a"
    cli.add_worker_workspace(root, "tag-research", folder)
    update_worker_settings(root, enabled=True, paused=True)
    cli.set_worker_account(root, "1")
    policy = load_worker_settings(root)
    assert policy.enabled is True and policy.paused is True
    assert [w.workspace_id for w in policy.workspaces] == ["research", "tag-research"]
    cli.set_worker_account(root, None)
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["research", "tag-research"]


def test_a_removed_pinned_account_is_reported(root, capsys):
    cli.set_worker_account(root, "1")
    roster = json.loads((root / "codex" / "sequence.json").read_text())
    del roster["accounts"]["1"]
    (root / "codex" / "sequence.json").write_text(json.dumps(roster))
    assert _run(root, "account") == 0
    assert "no longer in the Codex roster" in capsys.readouterr().out
    assert _run(root, "account", "--json") == 0
    assert json.loads(capsys.readouterr().out)["pinned_missing"] is True


# --- openswap worker workspace --------------------------------------------------


def test_workspace_add_list_remove(root, tmp_path, capsys):
    folder = tmp_path / "research-a"
    assert _run(root, "workspace", "add", "tag-research", str(folder)) == 0
    assert "as workspace 'tag-research'" in capsys.readouterr().out
    assert folder.is_dir()
    if os.name == "posix":
        assert folder.stat().st_mode & 0o777 == 0o700

    assert _run(root, "workspace", "list", "--json") == 0
    listed = json.loads(capsys.readouterr().out)["workspaces"]
    assert [item["workspace_id"] for item in listed] == ["research", "tag-research"]
    assert listed[1]["output_root"] == str(folder.resolve())

    assert _run(root, "workspace", "remove", "research") == 0
    assert "folder and files are unchanged" in capsys.readouterr().out
    assert _run(root, "workspace", "remove", "tag-research", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "last_workspace"
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["tag-research"]
    assert folder.is_dir()


def test_workspace_add_with_a_readonly_source(root, tmp_path, capsys):
    source = tmp_path / "checkout"
    source.mkdir()
    os.chmod(source, 0o755)
    folder = tmp_path / "bugs"
    assert _run(root, "workspace", "add", "bugs", str(folder), "--readonly-source", str(source), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workspace"]["readonly_roots"] == [str(source.resolve())]
    workspace = next(w for w in load_worker_settings(root).workspaces if w.workspace_id == "bugs")
    assert workspace == WorkerWorkspace("bugs", folder.resolve(), (source.resolve(),))


@pytest.mark.parametrize(("args", "code"), [
    (("Bad_ID", "{tmp}/x"), "workspace_id_invalid"),
    (("research", "{tmp}/x"), "workspace_exists"),
    (("nest", "{tmp}/out", "--readonly-source", "{tmp}/out/src"), "readonly_source_unavailable"),
    (("nest", "{tmp}/checkout/out", "--readonly-source", "{tmp}/checkout"), "readonly_source_overlaps_folder"),
    (("home", "{home}"), "folder_exposes_credentials"),
    (("above", "{tmp}"), "folder_exposes_credentials"),
    (("codex", "{home}/.codex"), "folder_exposes_credentials"),
    (("creds", "{root}/codex/slots"), "folder_exposes_credentials"),
    (("src", "{tmp}/fine", "--readonly-source", "{root}/codex"), "readonly_source_exposes_credentials"),
])
def test_workspace_add_refusals(root, tmp_path, capsys, args, code):
    if code == "readonly_source_overlaps_folder":
        (tmp_path / "checkout").mkdir(exist_ok=True)
    values = {"tmp": str(tmp_path), "home": str(Path.home()), "root": str(root)}
    argv = [item.format(**values) for item in args]
    before = load_worker_settings(root).workspaces
    assert _run(root, "workspace", "add", *argv, "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == code
    assert load_worker_settings(root).workspaces == before


def test_workspace_add_refuses_a_credential_home_inside_the_worker_directory(root, monkeypatch, capsys):
    """The `worker` exception belongs to the backup root alone: a Codex home
    configured beneath it is still refused, while a plain folder there is fine."""
    import openswap.codex.auth as codex_auth

    (root / "worker").mkdir(mode=0o700, exist_ok=True)
    codex = root / "worker" / "codex-home"
    for path in (codex, codex / "slots", codex / "slots" / "1"):
        path.mkdir(mode=0o700)
    monkeypatch.setattr(codex_auth, "codex_home", lambda: codex)
    assert _run(root, "workspace", "add", "slot", str(codex / "slots" / "1"), "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "folder_exposes_credentials"
    assert _run(root, "workspace", "add", "plain", str(root / "worker" / "plain"), "--json") == 0
    capsys.readouterr()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_workspace_add_refuses_folders_others_can_access(root, tmp_path, capsys):
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o755)
    assert _run(root, "workspace", "add", "shared", str(shared)) == 1
    assert "chmod 700" in capsys.readouterr().err
    writable_source = tmp_path / "writable-source"
    writable_source.mkdir()
    os.chmod(writable_source, 0o777)
    assert _run(root, "workspace", "add", "ok", str(tmp_path / "ok"),
                "--readonly-source", str(writable_source), "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "readonly_source_permissions"
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["research"]


def test_workspace_remove_unknown_and_lock(root, monkeypatch, capsys):
    assert _run(root, "workspace", "remove", "nope", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "workspace_not_found"
    monkeypatch.setattr(cli, "_LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.1)
    (root / "worker").mkdir(mode=0o700, exist_ok=True)
    with FileLock(root / "worker" / "lifecycle.lock"):
        assert _run(root, "workspace", "remove", "research", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "worker_lifecycle_busy"


def test_workspace_removal_waits_for_jobs_that_still_need_the_folder(root, tmp_path, capsys):
    """Artifact upload reads the job's folder from the registry, so a workspace
    a queued, running or not-yet-synchronized job uses cannot be removed."""
    import sqlite3

    from openswap.worker.journal import LocalJobStore

    assert _run(root, "workspace", "add", "alt", str(tmp_path / "alt")) == 0
    store = LocalJobStore(root)
    epoch = store.current_epoch()
    job = store.create(_submission(), owner_ref="local-user", worker_epoch=epoch)
    assert job.workspace_id == "research"
    capsys.readouterr()
    assert _run(root, "workspace", "remove", "research", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "workspace_in_use"

    store.transition(job.job_id, expected_states=(JobState.QUEUED,), new_state=JobState.CANCELLED,
                     worker_epoch=epoch, expected_generation=job.generation)
    remote = store.state_dir / "remote.sqlite3"
    db = sqlite3.connect(remote)
    db.execute("CREATE TABLE bindings (service TEXT, remote_id TEXT, claim TEXT NOT NULL, local_id TEXT, "
               "cursor INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(service,remote_id))")
    db.execute("INSERT INTO bindings(service,remote_id,claim,local_id) VALUES ('s','r','{}',?)", (job.job_id,))
    db.commit()
    assert _run(root, "workspace", "remove", "research", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "workspace_in_use"

    # A claim remembered but not yet admitted (no local job yet) still holds its folder.
    db.execute("UPDATE bindings SET done=1")
    db.execute("INSERT INTO bindings(service,remote_id,claim) VALUES ('s','r2',?)",
               (json.dumps({"job_id": "r2", "submission": {"workspace_id": "research"}}),))
    db.commit()
    assert _run(root, "workspace", "remove", "research", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "workspace_in_use"

    db.execute("UPDATE bindings SET done=1")
    db.commit()
    db.close()
    assert _run(root, "workspace", "remove", "research", "--json") == 0
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["alt"]


def test_workspace_changes_keep_the_pin(root, tmp_path):
    cli.set_worker_account(root, "1")
    cli.add_worker_workspace(root, "tag-research", tmp_path / "a")
    cli.remove_worker_workspace(root, "research")
    assert load_worker_settings(root).pinned_account_ref == ALICE


# --- the worker reads the pin per job --------------------------------------------


class _FinishingAdapter(_FakeAdapter):
    """Finishes every run with stop proof; records the lease identity at start."""

    def __init__(self, root, on_start=None):
        super().__init__()
        self.root = root
        self.on_start = on_start
        self.leased = []

    def start(self, job, workspace, *, worker_epoch):
        self.leased.append(AccountLeaseStore(self.root, "codex").current().account_identity)
        if self.on_start is not None:
            self.on_start()
        self.events_out = (_finished_event(job.job_id),)
        return super().start(job, workspace, worker_epoch=worker_epoch)


def test_a_changed_pin_applies_to_the_next_job_without_a_restart(root):
    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    adapter = _FinishingAdapter(root, on_start=lambda: cli.set_worker_account(root, "2"))
    runtime = WorkerRuntime(root, adapter=adapter)

    first = runtime.submit(_submission("first"))
    done = runtime.reconcile_once()
    assert done.job_id == first.job_id and done.state == JobState.SUCCEEDED
    # The pin changed while the job ran; that job kept the account it started with.
    assert runtime.get(first.job_id).pinned_account_ref == ALICE
    assert adapter.leased == [ALICE]

    adapter.on_start = None
    second = runtime.submit(_submission("second"))
    assert runtime.reconcile_once().state == JobState.SUCCEEDED
    assert runtime.get(second.job_id).pinned_account_ref == BOB
    assert adapter.leased == [ALICE, BOB]


def test_a_pin_change_just_before_the_launch_takes_its_locks_is_honoured(root, monkeypatch):
    """The pin is read under the lifecycle lock and the Codex mutation guard:
    a change that completes right before the launch takes them is the account
    the job uses (one that arrives later waits for the lease to be taken)."""
    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    adapter = _FinishingAdapter(root)
    runtime = WorkerRuntime(root, adapter=adapter)
    real_probe = adapter.probe
    raced = []

    def racing_probe(*args, **kwargs):
        if not raced:
            raced.append(True)
            cli.set_worker_account(root, "2")  # the owner's change lands first
        return real_probe(*args, **kwargs)

    monkeypatch.setattr(adapter, "probe", racing_probe)
    job = runtime.submit(_submission())
    assert runtime.reconcile_once().state == JobState.SUCCEEDED
    assert raced == [True]
    assert runtime.get(job.job_id).pinned_account_ref == BOB
    assert adapter.leased == [BOB]


def test_an_owner_command_holding_the_lifecycle_lock_waits_out_the_launch(root, monkeypatch):
    """Lock order: the launch takes the lifecycle lock before _launch_lock, so
    a stop arriving over IPC while an owner command holds the lifecycle lock
    still gets _launch_lock, and the launch waits instead of deadlocking."""
    import threading as _threading

    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    adapter = _FinishingAdapter(root)
    runtime = WorkerRuntime(root, adapter=adapter)
    holding, release = _threading.Event(), _threading.Event()
    got_launch_lock = []

    def owner_command():
        with cli.lifecycle_lock(root):
            holding.set()
            release.wait(5)
            time.sleep(0.3)  # the launch is now waiting for the lifecycle lock
            # What a disable's IPC stop handler does in the worker process.
            acquired = runtime._launch_lock.acquire(timeout=2)
            got_launch_lock.append(acquired)
            if acquired:
                runtime._launch_lock.release()

    real_probe = adapter.probe

    def probe_then_let_owner_in(*args, **kwargs):
        result = real_probe(*args, **kwargs)
        command.start()
        holding.wait(5)
        release.set()
        return result

    command = _threading.Thread(target=owner_command)
    monkeypatch.setattr(adapter, "probe", probe_then_let_owner_in)
    runtime.submit(_submission())
    assert runtime.reconcile_once().state == JobState.SUCCEEDED
    command.join(5)
    assert got_launch_lock == [True]


def test_clearing_the_pin_fails_the_next_job_as_before(root):
    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    adapter = _FinishingAdapter(root)
    runtime = WorkerRuntime(root, adapter=adapter)
    cli.set_worker_account(root, None)
    runtime.submit(_submission())
    result = runtime.reconcile_once()
    assert result.state == JobState.FAILED and result.diagnostic_code == "provider_unavailable"
    assert adapter.start_count == 0


def test_a_pinned_account_removed_from_the_roster_fails_the_launch_safely(root):
    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    adapter = _FinishingAdapter(root)
    runtime = WorkerRuntime(root, adapter=adapter)
    assert runtime.account_ready() is True
    _write_codex_roster(root, {
        "2": {"email": "bob@example.com", "accountId": "acct-bob", "kind": "oauth"},
    })
    assert runtime.account_ready() is False
    assert codex_account_in_roster(root, ALICE) is False

    job = runtime.submit(_submission())
    result = runtime.reconcile_once()

    assert result.state == JobState.FAILED
    assert result.diagnostic_code == "provider_auth_unavailable"
    assert adapter.start_count == 0
    # Nothing was leased, so the account roster stays free for the owner.
    assert AccountLeaseStore(root, "codex").current() is None
    # The code is on the journal/protocol allowlist, not a new one.
    assert validate_event_fields(
        kind=SafeEventKind.STATE_CHANGED, state=JobState.FAILED,
        diagnostic_code="provider_auth_unavailable", execution_stopped=False,
    ) == "provider_auth_unavailable"
    events = runtime.events(job.job_id).events
    assert events[-1].diagnostic_code == "provider_auth_unavailable"


def test_an_unreadable_roster_fails_closed(root):
    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    (root / "codex" / "sequence.json").write_text("{not json", encoding="utf-8")
    adapter = _FinishingAdapter(root)
    runtime = WorkerRuntime(root, adapter=adapter)
    runtime.submit(_submission())
    result = runtime.reconcile_once()
    assert result.diagnostic_code == "provider_auth_unavailable"
    assert adapter.start_count == 0


# --- worker status: paired but off -----------------------------------------------------


def _status(root, monkeypatch, capsys, snapshot, *extra):
    monkeypatch.setattr(cli, "read_status", lambda _root: snapshot)
    assert _run(root, "status", *extra) == 0
    return capsys.readouterr().out


@pytest.mark.parametrize("snapshot", [
    {"enabled": False, "process_state": "stopped"},
    {"enabled": True, "process_state": "stopped"},
    {"enabled": True, "process_state": "stale"},
])
def test_status_hints_at_enable_when_paired_but_off(root, monkeypatch, capsys, snapshot):
    configure_worker_service(root, "https://tag.example.com", "worker-1")
    out = _status(root, monkeypatch, capsys, snapshot)
    assert out.splitlines()[-1] == (
        "Paired with https://tag.example.com but the worker is off; run `openswap worker enable`."
    )


@pytest.mark.parametrize("paired, snapshot", [
    (False, {"enabled": False, "process_state": "stopped"}),
    (True, {"enabled": True, "process_state": "running"}),
])
def test_status_has_no_hint_when_unpaired_or_running(root, monkeypatch, capsys, paired, snapshot):
    if paired:
        configure_worker_service(root, "https://tag.example.com", "worker-1")
    out = _status(root, monkeypatch, capsys, snapshot)
    assert "Paired with" not in out and len(out.splitlines()) == 1


def test_status_json_is_unchanged_when_paired_but_off(root, monkeypatch, capsys):
    configure_worker_service(root, "https://tag.example.com", "worker-1")
    snapshot = {"enabled": False, "process_state": "stopped"}
    out = _status(root, monkeypatch, capsys, snapshot, "--json")
    assert json.loads(out) == snapshot


# --- menu bar: paired, worker off ------------------------------------------------------


def _general_rows(**kwargs):
    return menubar.settings_page_rows(
        menubar.MenuBarSettings(), strategy="best", threshold=90,
        section=menubar.SETTINGS_SECTION_GENERAL, **kwargs,
    )


def test_menu_says_paired_worker_off_right_under_the_enable_switch():
    rows = _general_rows(worker_paired=True, worker_enabled=False)
    ids = [row["id"] for row in rows]
    assert ids.index("remote_tasks_paired_off") == ids.index("remote_tasks_enabled") + 1
    hint = rows[ids.index("remote_tasks_paired_off")]
    assert hint["kind"] == "group" and hint["style"] == "hint"
    assert hint["label"].startswith("Paired, worker off")
    assert [row["id"] for row in rows if row.get("kind") == "toggle"].count("remote_tasks_enabled") == 1


@pytest.mark.parametrize("kwargs", [
    {"worker_paired": False, "worker_enabled": False},
    {"worker_paired": True, "worker_enabled": True},
    {"worker_paired": True, "worker_enabled": False,
     "worker_status": {"operation": "worker_enable_or_disable"}},
])
def test_menu_has_no_paired_off_line_otherwise(kwargs):
    assert "remote_tasks_paired_off" not in [row["id"] for row in _general_rows(**kwargs)]


# --- menu bar account picker ---------------------------------------------------------


def _picker(root):
    return cli.worker_account_choices(root).to_dict()


def test_menu_account_row_marks_pin_and_offers_claude(root):
    cli.set_worker_account(root, "1")
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(), strategy="best", threshold=90,
        worker_status={"account_picker": _picker(root)},
        section=menubar.SETTINGS_SECTION_GENERAL,
    )
    ids = [row["id"] for row in rows]
    assert ids.index("remote_tasks_enabled") < ids.index("remote_tasks_account") < ids.index("remote_tasks_status")
    row = rows[ids.index("remote_tasks_account")]
    assert row["kind"] == "popup" and row["value"] == ALICE
    options = {option[0]: option for option in row["options"]}
    assert options[""][1] == "None" and len(options[""]) == 2
    assert options[ALICE][1] == "Codex 1 · alice@example.com (work)" and len(options[ALICE]) == 2
    assert options[BOB][1] == "Codex 2 · bob@example.com"
    assert options["ineligible:3"][2] == {"disabled": True}
    claude = [option for option in row["options"] if str(option[0]).startswith("claude:")]
    assert [option[1] for option in claude] == [
        "Claude 1 · alice@example.com",
        "Claude 4 · carol@example.com (claudey)",
    ]
    assert all(len(option) == 2 for option in claude)  # enabled
    assert SECRET not in json.dumps(row)


def test_menu_account_row_placeholder_and_removed_pin(root):
    loading = menubar_display_row(None)
    assert loading["disabled"] is True and loading["options"] == [("", "Loading accounts…")]

    cli.set_worker_account(root, "1")
    _write_codex_roster(root, {"2": {"email": "bob@example.com", "accountId": "acct-bob"}})
    row = menubar_display_row(_picker(root))
    assert row["value"] == ALICE
    assert (ALICE, "Pinned account was removed", {"disabled": True}) in row["options"]


def menubar_display_row(picker):
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(), strategy="best", threshold=90,
        worker_status={} if picker is None else {"account_picker": picker},
        section=menubar.SETTINGS_SECTION_GENERAL,
    )
    return next(row for row in rows if row["id"] == "remote_tasks_account")


def _menu_app(root):
    from tests.menubar_harness import extract_class

    app_type = extract_class(
        menubar.__file__, "MenuBarApp",
        {"_worker_action", "_worker_action_worker", "_pin_worker_account",
         "_with_account_picker", "_drain_worker_result", "_on_setting"},
        {"threading": threading},
    )
    app = app_type()
    app.switcher = SimpleNamespace(backup_dir=root)
    app._worker_generation = 0
    app._worker_result_lock = threading.Lock()
    app._worker_status_inflight = False
    app._worker_operation = None
    app._worker_policy = WorkerSettings()
    app._worker_status_cache = {"process_state": "unavailable"}
    app._worker_result = None
    app._panel = None
    return app


def test_menu_selection_pins_off_the_ui_thread_through_the_cli_function(root, monkeypatch):
    monkeypatch.setattr("openswap.worker.cli.read_status", lambda _root: {"process_state": "stopped"})
    calls = []
    original = cli.set_worker_account

    def spy(backup_root, selector):
        calls.append((threading.current_thread() is threading.main_thread(), selector))
        return original(backup_root, selector)

    monkeypatch.setattr(cli, "set_worker_account", spy)
    app = _menu_app(root)

    app._on_setting("remote_tasks_account", BOB)
    assert app._worker_status_cache["operation"] == "worker_account_update"
    for _ in range(200):
        if app._worker_result is not None:
            break
        threading.Event().wait(0.01)
    app._drain_worker_result()

    assert calls == [(False, BOB)]
    assert load_worker_settings(root).pinned_account_ref == BOB
    assert app._worker_policy.pinned_account_ref == BOB
    assert app._worker_status_cache["account_picker"]["pinned_account_ref"] == BOB
    assert app._worker_status_cache["diagnostic_notice"] is None


def test_menu_pins_claude_refuses_stray_values_and_clears_with_none(root):
    app = _menu_app(root)
    assert app._pin_worker_account(root, CAROL) is None
    assert load_worker_settings(root).pinned_account_ref == CAROL
    cli.set_worker_account(root, "1")
    assert app._pin_worker_account(root, "claude:4") == "account_not_eligible"  # not a reference
    assert app._pin_worker_account(root, "ineligible:3") == "account_not_eligible"
    assert app._pin_worker_account(root, None) == "account_not_eligible"
    assert app._pin_worker_account(root, stable_account_identity("codex", "gone")) == "account_not_found"
    assert load_worker_settings(root).pinned_account_ref == ALICE
    assert app._pin_worker_account(root, "") is None
    assert load_worker_settings(root).pinned_account_ref is None


def test_menu_status_copy_names_the_account_operation():
    from openswap.menubar_display import _remote_tasks_status_copy

    text = _remote_tasks_status_copy({"operation": "worker_account_update"}, enabled=True, paused=False)
    assert text.endswith("· worker_account_update")
