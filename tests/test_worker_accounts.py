"""The owner's local account pin and approved research folders for Remote tasks.

Roster files here hold metadata only; one slot also has a synthetic auth file
whose token must never be read into any output. No Keychain, launchctl or
provider auth is touched.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap.locking import FileLock
from openswap.settings import (
    WorkerSettings,
    WorkerWorkspace,
    configure_worker_local_policy,
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


def test_account_list_marks_the_pin_and_lists_claude_as_not_eligible(root, capsys):
    assert _run(root, "account") == 0
    out = capsys.readouterr().out
    assert "1 · alice@example.com (work)" in out
    assert "3 · (no email)  [not eligible: no ChatGPT account ID (API key)]" in out
    assert "Claude accounts (not eligible yet: Claude authentication gate):" in out
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
    assert all(row["eligible"] is False for row in payload["claude"])
    assert {row["number"] for row in payload["claude"]} == {"1", "4"}


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
        "number": "2", "email": "bob@example.com", "alias": None, "account_ref": BOB,
    }}
    assert _run(root, "account", "--clear") == 0
    assert "Cleared" in capsys.readouterr().out
    assert load_worker_settings(root).pinned_account_ref is None


@pytest.mark.parametrize(("selector", "code"), [
    ("claudey", "claude_not_supported"),
    ("4", "claude_not_supported"),
    ("carol@example.com", "claude_not_supported"),
    ("claude:1", "claude_not_supported"),
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


def test_a_claude_account_can_never_be_pinned_even_by_reference(root):
    claude_ref = stable_account_identity("claude", "carol@example.com", "")
    with pytest.raises(AccountPinError) as refused:
        cli.set_worker_account(root, claude_ref)
    assert refused.value.code == "claude_not_supported"
    with pytest.raises(ValueError):
        # The settings layer itself only stores codex: references.
        configure_worker_local_policy(root, pinned_account_ref=claude_ref,
                                      workspaces=load_worker_settings(root).workspaces)
    assert load_worker_settings(root).pinned_account_ref is None


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


# --- after pairing ----------------------------------------------------------------


def _pair(root, monkeypatch, *, interactive, answers=()):
    monkeypatch.setattr(pairing, "Transport", lambda *_: PairTransport())
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: interactive)
    replies = iter(answers)

    def fake_input(prompt=""):
        print(prompt, end="")
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    return cli.main(["pair", "http://localhost", "one-use"], backup_root=root)


def test_pair_on_a_tty_offers_eligible_codex_accounts(root, keychain, monkeypatch, capsys):
    assert _pair(root, monkeypatch, interactive=True, answers=["claudey", "2"]) == 0
    out = capsys.readouterr().out
    assert "Paired worker worker." in out
    assert "  1 · alice@example.com (work)" in out
    assert "  3 · " not in out  # an API-key slot is not offered
    assert "carol@example.com" not in out  # Claude is not offered
    assert cli._ACCOUNT_MESSAGES["claude_not_supported"] in out
    assert "Pinned Codex account 2 · bob@example.com" in out
    assert "openswap worker workspace add <id> <folder>" in out
    assert SECRET not in out
    assert load_worker_settings(root).pinned_account_ref == BOB


def test_pair_on_a_tty_enter_skips(root, keychain, monkeypatch, capsys):
    assert _pair(root, monkeypatch, interactive=True, answers=[""]) == 0
    out = capsys.readouterr().out
    assert "Skipped." in out and "openswap worker workspace add" in out
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref is None
    assert policy.control_service_url == "http://localhost"


def test_pair_on_a_tty_with_a_pin_does_not_prompt(root, keychain, monkeypatch, capsys):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=[]) == 0
    out = capsys.readouterr().out
    assert "Remote tasks uses Codex account 1 · alice@example.com (work)." in out
    assert "Account (slot" not in out
    assert "openswap worker workspace add" in out


def test_pair_without_a_tty_prints_next_steps(root, keychain, monkeypatch, capsys):
    assert _pair(root, monkeypatch, interactive=False,
                 answers=["1"]) == 0  # an answer is never read without a TTY
    out = capsys.readouterr().out
    assert "Account (slot" not in out
    assert "Next: pin the Codex account" in out
    assert "openswap worker workspace add <id> <folder>" in out
    assert load_worker_settings(root).pinned_account_ref is None


def test_pairing_succeeds_even_if_the_follow_up_fails(root, keychain, monkeypatch, capsys):
    def broken(*_args, **_kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(cli, "_post_pair_setup", broken)
    assert _pair(root, monkeypatch, interactive=True) == 0
    assert "openswap worker account" in capsys.readouterr().out
    assert load_worker_settings(root).control_service_url == "http://localhost"


def test_pair_prompt_survives_a_failing_pin(root, keychain, monkeypatch, capsys):
    monkeypatch.setattr(cli, "set_worker_account",
                        lambda *_: (_ for _ in ()).throw(OSError("disk")))
    assert _pair(root, monkeypatch, interactive=True, answers=["1"]) == 0
    assert "Could not pin that account" in capsys.readouterr().out


# --- menu bar account picker ---------------------------------------------------------


def _picker(root):
    return cli.worker_account_choices(root).to_dict()


def test_menu_account_row_marks_pin_and_disables_claude(root):
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
    assert options[ALICE][1] == "1 · alice@example.com (work)" and len(options[ALICE]) == 2
    assert options[BOB][1] == "2 · bob@example.com"
    assert options["ineligible:3"][2] == {"disabled": True}
    claude = [option for option in row["options"] if str(option[0]).startswith("claude:")]
    assert [option[1] for option in claude] == [
        "Claude 1 · alice@example.com — not supported yet",
        "Claude 4 · carol@example.com — not supported yet",
    ]
    assert all(option[2] == {"disabled": True} for option in claude)
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


def test_menu_refuses_claude_and_stray_values_and_clears_with_none(root):
    app = _menu_app(root)
    cli.set_worker_account(root, "1")
    assert app._pin_worker_account(root, "claude:4") == "claude_not_supported"
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
