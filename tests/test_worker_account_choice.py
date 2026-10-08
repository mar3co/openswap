"""Optional per-job account choice (worker protocol v1 extension).

The owner's local allowlist, its CLI and menu rows, the ``accounts``
operation on the client and the reference server, and how the worker resolves
a claim's ``account_ref`` before launch. Roster files hold metadata only; no
Keychain, launchctl or provider auth is touched.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import threading

import pytest

from openswap import menubar
from openswap.settings import (
    AccountAllowlistFullError,
    configure_worker_service,
    load_worker_settings,
    set_worker_pinned_account,
    settings_path,
    update_worker_settings,
)
from openswap.worker import cli
from openswap.worker.leases import AccountLeaseStore, stable_account_identity
from openswap.worker.models import JobState, JobSubmission
from openswap.worker.protocol import (
    AdvertisedAccount, Claim, ProtocolError, Submission, advertised_accounts,
)
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker.remote import RemoteClient, Transport
from openswap.worker.runtime import WorkerRuntime
from tests.test_worker_accounts import (  # noqa: F401 (fixture)
    ALICE, BOB, SECRET, _menu_app, _write_codex_roster, root,
)
from tests.test_worker_pairing_status import keychain  # noqa: F401 (fixture)
from tests.test_worker_remote import FakeAdapter, StoreTransport

URL = "http://127.0.0.1:8765"
REF = re.compile(r"^[0-9a-f]{32}$")


def _run(root, *argv):
    return cli.main(list(argv), backup_root=root)


def _entry(root, identity):
    return next(e for e in load_worker_settings(root).account_allowlist if e.identity == identity)


def _write_legacy_pin(root, identity):
    """Settings as #76 wrote them: a pin and no allowlist."""
    path = settings_path(root)
    raw = json.loads(path.read_text()) if path.exists() else {}
    raw.setdefault("worker", {})["pinnedAccountRef"] = identity
    raw["worker"].pop("accountAllowlist", None)
    path.write_text(json.dumps(raw))


# --- settings: allowlist, migration and validation ----------------------------------


def test_a_pin_without_an_allowlist_loads_and_migrates_on_the_next_write(root):
    update_worker_settings(root, enabled=False)
    _write_legacy_pin(root, ALICE)
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref == ALICE and policy.account_allowlist == ()

    update_worker_settings(root, paused=True)  # any worker-section write
    (entry,) = load_worker_settings(root).account_allowlist
    assert entry.identity == ALICE and entry.label == "work"  # the slot alias
    assert REF.fullmatch(entry.account_ref)
    assert entry.account_ref not in ALICE and "acct-alice" not in entry.account_ref
    saved = json.loads(settings_path(root).read_text())["worker"]["accountAllowlist"]
    assert saved == [{"accountRef": entry.account_ref, "identity": ALICE, "label": "work"}]
    # Later writes keep the generated reference.
    update_worker_settings(root, paused=False)
    assert load_worker_settings(root).account_allowlist == (entry,)


def test_migration_label_is_never_the_email_and_refs_are_random(root, tmp_path):
    update_worker_settings(root, enabled=False)
    _write_legacy_pin(root, BOB)  # slot 2 has an email and no alias
    configure_worker_service(root, URL)  # the pairing write migrates too
    (entry,) = load_worker_settings(root).account_allowlist
    assert entry.label == "Codex account 2" and "@" not in entry.label

    other = tmp_path / "other"
    other.mkdir()
    _write_codex_roster(other, {"2": {"email": "bob@example.com", "accountId": "acct-bob"}})
    update_worker_settings(other, enabled=False)
    _write_legacy_pin(other, BOB)
    update_worker_settings(other, enabled=False)
    (again,) = load_worker_settings(other).account_allowlist
    # Same account, different Mac or settings: a different, uncorrelatable reference.
    assert again.identity == BOB and again.account_ref != entry.account_ref


def test_clearing_a_pre_allowlist_pin_does_not_allowlist_it(root):
    update_worker_settings(root, enabled=False)
    _write_legacy_pin(root, ALICE)
    cli.set_worker_account(root, None)
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref is None and policy.account_allowlist == ()


def test_pinning_adds_the_account_to_the_allowlist_once(root, capsys):
    assert _run(root, "account", "2") == 0
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref == BOB
    (entry,) = policy.account_allowlist
    assert entry.identity == BOB and entry.label == "Codex account 2"
    cli.set_worker_account(root, "1")
    cli.set_worker_account(root, "2")
    policy = load_worker_settings(root)
    assert [e.identity for e in policy.account_allowlist] == [BOB, ALICE]
    assert _entry(root, BOB).account_ref == entry.account_ref  # kept, not regenerated
    # Clearing the pin keeps both allowed; there is just no default.
    cli.set_worker_account(root, None)
    assert [e.identity for e in load_worker_settings(root).account_allowlist] == [BOB, ALICE]


@pytest.mark.parametrize("mutate", [
    lambda entries: entries.append(dict(entries[0])),  # duplicate reference and identity
    lambda entries: entries[0].update(accountRef="not-hex"),
    lambda entries: entries[0].update(accountRef=entries[0]["accountRef"].upper()),
    lambda entries: entries[0].update(identity="claude:" + "0" * 64),
    lambda entries: entries[0].update(label=""),
    lambda entries: entries[0].update(label="x" * 101),
    lambda entries: entries[0].update(label="tab\there"),
    lambda entries: entries[0].update(label="del\x7f"),
    lambda entries: entries[0].update(extra=1),
    lambda entries: entries[0].pop("label"),
    lambda entries: entries.clear() or entries.append(
        {"accountRef": "0" * 32, "identity": BOB, "label": "pin is missing"}),
    lambda entries: entries.clear(),  # explicitly empty while a default is pinned
    lambda entries: entries.extend(
        {"accountRef": f"{i:032x}", "identity": "codex:" + f"{i:064x}", "label": f"n{i}"}
        for i in range(1, 21)),
])
def test_an_invalid_allowlist_fails_closed(root, mutate):
    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    raw = json.loads(settings_path(root).read_text())
    mutate(raw["worker"]["accountAllowlist"])
    settings_path(root).write_text(json.dumps(raw))
    policy = load_worker_settings(root)
    assert policy.enabled is False and policy.pinned_account_ref is None and policy.account_allowlist == ()


def test_an_explicit_null_allowlist_fails_closed_unlike_a_missing_key(root):
    """Only a missing key gets the legacy exemption; JSON null is malformed."""
    update_worker_settings(root, enabled=True)
    cli.set_worker_account(root, "1")
    raw = json.loads(settings_path(root).read_text())
    raw["worker"]["accountAllowlist"] = None
    settings_path(root).write_text(json.dumps(raw))
    policy = load_worker_settings(root)
    assert policy.enabled is False and policy.pinned_account_ref is None
    del raw["worker"]["accountAllowlist"]
    settings_path(root).write_text(json.dumps(raw))
    policy = load_worker_settings(root)
    assert policy.enabled is True and policy.pinned_account_ref == ALICE


def test_the_settings_layer_refuses_a_twenty_first_account(root):
    accounts = {str(n): {"email": f"u{n}@example.com", "accountId": f"acct-{n}"} for n in range(1, 22)}
    _write_codex_roster(root, accounts)
    for n in range(1, 21):
        cli.allow_worker_account(root, str(n))
    with pytest.raises(AccountAllowlistFullError):
        set_worker_pinned_account(root, stable_account_identity("codex", "acct-21"))
    assert len(load_worker_settings(root).account_allowlist) == 20
    assert load_worker_settings(root).pinned_account_ref is None


# --- CLI ---------------------------------------------------------------------------------


def test_allow_label_and_list(root, capsys):
    cli.set_worker_account(root, "1")
    assert _run(root, "account", "allow", "2", "--label", "Bob's research", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    bob = _entry(root, BOB)
    assert payload == {"accepted": True, "pinned_account_ref": ALICE, "account": {
        "account_ref": bob.account_ref, "identity": BOB, "label": "Bob's research", "default": False,
    }}
    # Allowing again keeps the reference; a new label replaces the old one.
    assert _run(root, "account", "allow", "bob@example.com") == 0
    assert _entry(root, BOB) == bob
    assert _run(root, "account", "label", bob.account_ref, "Second") == 0
    assert "as \"Second\"" in capsys.readouterr().out
    assert _entry(root, BOB).account_ref == bob.account_ref and _entry(root, BOB).label == "Second"
    assert _run(root, "account", "label", "work", "Main") == 0  # a roster selector works too

    capsys.readouterr()
    assert _run(root, "account") == 0
    out = capsys.readouterr().out
    alice = _entry(root, ALICE)
    assert f"  ✓ {alice.account_ref}  \"Main\"    Codex slot 1  default" in out
    assert f"  • {bob.account_ref}  \"Second\"  Codex slot 2" in out
    assert SECRET not in out

    assert _run(root, "account", "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["allowlist"] == [
        {"account_ref": alice.account_ref, "identity": ALICE, "label": "Main", "default": True,
         "provider": "codex", "slot": "1", "in_roster": True},
        {"account_ref": bob.account_ref, "identity": BOB, "label": "Second", "default": False,
         "provider": "codex", "slot": "2", "in_roster": True},
    ]
    assert {row["number"]: row["allowed"] for row in listed["codex"]} == {
        "1": True, "2": True, "3": False, "5": False, "6": False,
    }


def test_the_email_is_a_label_only_when_the_owner_passes_it(root):
    cli.allow_worker_account(root, "bob@example.com")
    assert _entry(root, BOB).label == "Codex account 2"
    cli.allow_worker_account(root, "2", label="bob@example.com")
    assert _entry(root, BOB).label == "bob@example.com"


@pytest.mark.parametrize(("argv", "code"), [
    (("allow", "claude:9"), "account_not_found"),
    (("allow", "3"), "account_not_eligible"),
    (("allow", "9"), "account_not_found"),
    (("allow", "2", "--label", ""), "label_invalid"),
    (("allow", "2", "--label", "x" * 101), "label_invalid"),
    (("allow", "2", "--label", "bell\x07"), "label_invalid"),
    (("disallow", "2"), "account_not_allowlisted"),
    (("disallow", "0" * 32), "account_not_allowlisted"),
    (("disallow", "1"), "account_is_default"),
    (("label", "2", "Name"), "account_not_allowlisted"),
    (("label", "1", "line\nbreak"), "label_invalid"),
])
def test_allowlist_refusals_change_nothing(root, capsys, argv, code):
    cli.set_worker_account(root, "1")
    before = load_worker_settings(root)
    assert _run(root, "account", *argv, "--json") == 1
    assert json.loads(capsys.readouterr().out) == {"accepted": False, "diagnostic_code": code}
    assert _run(root, "account", *argv) == 1
    assert capsys.readouterr().err.strip() == cli._ACCOUNT_MESSAGES[code]
    assert load_worker_settings(root) == before


def test_disallowing_the_default_needs_clear_default(root, capsys):
    cli.set_worker_account(root, "1")
    cli.allow_worker_account(root, "2")
    alice = _entry(root, ALICE)
    assert _run(root, "account", "disallow", "work", "--clear-default", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["accepted"] is True and payload["removed"] is True
    assert payload["account"]["account_ref"] == alice.account_ref and payload["pinned_account_ref"] is None
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref is None and [e.identity for e in policy.account_allowlist] == [BOB]


def test_an_account_gone_from_the_roster_can_be_disallowed_by_reference(root, capsys):
    cli.set_worker_account(root, "1")
    cli.allow_worker_account(root, "2")
    bob = _entry(root, BOB)
    _write_codex_roster(root, {"1": {"email": "alice@example.com", "accountId": "acct-alice", "alias": "work"}})
    assert _run(root, "account") == 0
    assert f"  ✗ {bob.account_ref}  \"Codex account 2\"  no longer in the Codex roster" in capsys.readouterr().out
    assert _run(root, "account", "disallow", "2", "--json") == 1  # the slot is gone
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "account_not_found"
    assert _run(root, "account", "disallow", bob.account_ref) == 0
    assert [e.identity for e in load_worker_settings(root).account_allowlist] == [ALICE]


def test_at_most_twenty_accounts(root, capsys):
    accounts = {str(n): {"email": f"u{n}@example.com", "accountId": f"acct-{n}"} for n in range(1, 22)}
    _write_codex_roster(root, accounts)
    for n in range(1, 21):
        assert _run(root, "account", "allow", str(n), "--json") == 0
    capsys.readouterr()
    assert _run(root, "account", "allow", "21", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "too_many_accounts"
    assert _run(root, "account", "21", "--json") == 1  # pinning would add a 21st too
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "too_many_accounts"
    assert len(load_worker_settings(root).account_allowlist) == 20


def test_allowlist_changes_take_the_pin_locks(root, monkeypatch, capsys):
    from openswap.locking import FileLock

    monkeypatch.setattr(cli, "_LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.1)
    (root / "worker").mkdir(mode=0o700, exist_ok=True)
    with FileLock(root / "worker" / "lifecycle.lock"):
        assert _run(root, "account", "allow", "1", "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "worker_lifecycle_busy"
    with AccountLeaseStore(root, "codex").mutation_guard():
        assert _run(root, "account", "allow", "1", "--json") == 1
        assert json.loads(capsys.readouterr().out)["diagnostic_code"] == "account_roster_busy"
    assert load_worker_settings(root).account_allowlist == ()


# --- menu bar ----------------------------------------------------------------------------


def _web_choice_row(root):
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(), strategy="best", threshold=90,
        worker_status={"account_picker": cli.worker_account_choices(root).to_dict()},
        section=menubar.SETTINGS_SECTION_GENERAL,
    )
    ids = [row["id"] for row in rows]
    assert ids.index("remote_tasks_account") + 1 == ids.index("remote_tasks_web_choice")
    return rows[ids.index("remote_tasks_web_choice")]


def test_menu_web_choice_row_marks_allowed_and_default(root):
    cli.set_worker_account(root, "1")
    cli.allow_worker_account(root, "5")
    row = _web_choice_row(root)
    assert row["kind"] == "popup" and row["value"] == ""
    options = {option[0]: option for option in row["options"]}
    assert options[""][1] == "2 accounts allowed for web choice"
    assert options[f"default:{ALICE}"] == (f"default:{ALICE}", "✓ Codex 1 · alice@example.com (work) — default",
                                           {"disabled": True})
    five = stable_account_identity("codex", "acct-s1")
    assert options[f"disallow:{five}"][1] == "✓ Codex 5 · shared@example.com"
    assert options[f"allow:{BOB}"][1] == "   Codex 2 · bob@example.com"
    # Claude slots are offered too (owner decision 2026-10-07); API-key slots are not.
    claude = [option for value, option in options.items() if "claude:" in str(value)]
    assert [option[1] for option in claude] == ["   Claude 1 · alice@example.com", "   Claude 4 · carol@example.com (claudey)"]
    assert len(row["options"]) == 7  # the summary, Codex slots 1, 2, 5 and 6, Claude slots 1 and 4
    assert not any("3 · " in option[1] for option in options.values())
    assert SECRET not in json.dumps(row)


def test_menu_web_choice_row_placeholder_and_removed_account(root):
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(), strategy="best", threshold=90, worker_status={},
        section=menubar.SETTINGS_SECTION_GENERAL,
    )
    loading = next(row for row in rows if row["id"] == "remote_tasks_web_choice")
    assert loading["disabled"] is True
    cli.allow_worker_account(root, "2", label="Bob")
    _write_codex_roster(root, {"1": {"email": "alice@example.com", "accountId": "acct-alice"}})
    row = _web_choice_row(root)
    assert (f"disallow:{BOB}", "✓ Bob — removed from roster") in row["options"]
    assert row["options"][0] == ("", "1 account allowed for web choice")


def _menu(root):
    from tests.menubar_harness import extract_class

    app_type = extract_class(
        menubar.__file__, "MenuBarApp",
        {"_worker_action", "_worker_action_worker", "_pin_worker_account", "_toggle_web_choice",
         "_with_account_picker", "_drain_worker_result", "_on_setting"},
        {"threading": threading},
    )
    app = app_type()
    template = _menu_app(root)
    for name in ("switcher", "_worker_generation", "_worker_result_lock", "_worker_status_inflight",
                 "_worker_operation", "_worker_policy", "_worker_status_cache", "_worker_result", "_panel"):
        setattr(app, name, getattr(template, name))
    return app


def test_menu_toggles_off_the_ui_thread_through_the_cli_functions(root, monkeypatch):
    monkeypatch.setattr("openswap.worker.cli.read_status", lambda _root: {"process_state": "stopped"})
    calls = []
    original = cli.allow_worker_account

    def spy(backup_root, selector, label=None):
        calls.append((threading.current_thread() is threading.main_thread(), selector, label))
        return original(backup_root, selector, label)

    monkeypatch.setattr(cli, "allow_worker_account", spy)
    app = _menu(root)
    app._on_setting("remote_tasks_web_choice", "")  # the summary item: nothing happens
    assert app._worker_operation is None

    app._on_setting("remote_tasks_web_choice", f"allow:{BOB}")
    assert app._worker_status_cache["operation"] == "worker_account_update"
    for _ in range(200):
        if app._worker_result is not None:
            break
        threading.Event().wait(0.01)
    app._drain_worker_result()
    assert calls == [(False, BOB, None)]
    assert [e.identity for e in load_worker_settings(root).account_allowlist] == [BOB]
    picker = app._worker_status_cache["account_picker"]
    assert [entry["identity"] for entry in picker["allowlist"]] == [BOB]
    assert app._worker_status_cache["diagnostic_notice"] is None


def test_menu_toggle_refusals_and_disallow(root):
    app = _menu(root)
    cli.set_worker_account(root, "1")
    cli.allow_worker_account(root, "2")
    assert app._toggle_web_choice(root, f"disallow:{ALICE}") == "account_is_default"
    assert app._toggle_web_choice(root, f"default:{ALICE}") == "account_not_eligible"
    assert app._toggle_web_choice(root, "allow:claude:4") == "account_not_eligible"
    assert app._toggle_web_choice(root, None) == "account_not_eligible"
    assert app._toggle_web_choice(root, "allow:" + stable_account_identity("codex", "gone")) == "account_not_found"
    assert [e.identity for e in load_worker_settings(root).account_allowlist] == [ALICE, BOB]
    assert app._toggle_web_choice(root, f"disallow:{BOB}") is None
    assert [e.identity for e in load_worker_settings(root).account_allowlist] == [ALICE]


# --- protocol validation --------------------------------------------------------------------


def _accounts(*entries):
    return [{"account_ref": ref, "label": label, "default": default} for ref, label, default in entries]


def test_accounts_validation_accepts_bounded_closed_entries():
    assert advertised_accounts([]) == ()
    parsed = advertised_accounts(_accounts(("a", "Main", True), ("b" * 200, "x" * 100, False)))
    assert parsed == (AdvertisedAccount("a", "Main", True), AdvertisedAccount("b" * 200, "x" * 100, False))
    assert advertised_accounts(_accounts(*[(f"r{i}", "L", False) for i in range(20)]))


@pytest.mark.parametrize("value", [
    None, {}, "a",
    _accounts(*[(f"r{i}", "L", False) for i in range(21)]),
    _accounts(("a", "One", False), ("a", "Two", False)),
    _accounts(("a", "One", True), ("b", "Two", True)),
    _accounts(("a", "One", "true")),
    _accounts(("a", "One", 1)),
    _accounts(("", "One", False)),
    _accounts(("b" * 201, "One", False)),
    _accounts(("a", "", False)),
    _accounts(("a", "x" * 101, False)),
    _accounts(("a", "new\nline", False)),
    _accounts(("a", "del\x7fhere", False)),  # DEL
    _accounts(("a", "next\x85line", False)),  # C1 control (NEL)
    [{"account_ref": "a", "label": "One"}],
    [{"account_ref": "a", "label": "One", "default": False, "email": "x@example.com"}],
    ["a"],
])
def test_accounts_validation_refuses(value):
    with pytest.raises(ProtocolError, match="invalid_request"):
        advertised_accounts(value)


def _claim(account_ref=None):
    job = JobSubmission("idem", "codex", "Research", "research", "research",
                        datetime(2030, 1, 1, tzinfo=timezone.utc), 60)
    return {"job_id": "remote-1", "epoch": 1, "lease_until": "2030-01-01T00:00:00Z",
            "submission": Submission("worker", job, account_ref).to_dict()}


def test_claims_accept_account_ref_only_when_the_worker_advertised():
    assert Claim.from_dict(_claim()).submission.account_ref is None
    assert Claim.from_dict(_claim(), allow_account_ref=True).submission.account_ref is None
    with pytest.raises(ProtocolError, match="invalid_request"):
        Claim.from_dict(_claim("ref-1"))
    claim = Claim.from_dict(_claim("ref-1"), allow_account_ref=True)
    assert claim.submission.account_ref == "ref-1"
    assert claim.to_dict()["submission"]["account_ref"] == "ref-1"
    assert "account_ref" not in Claim.from_dict(_claim()).to_dict()["submission"]
    for bad in ("", "r" * 201, "tab\t", 7, None):
        value = _claim("x")
        value["submission"]["account_ref"] = bad
        with pytest.raises(ProtocolError, match="invalid_request"):
            Claim.from_dict(value, allow_account_ref=True)


# --- reference server --------------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    key = paired["device_key"]
    epoch = store.request("register", {}, key)["worker_epoch"]
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    return store, paired["worker_id"], key, epoch, ticks


def _service_submit(service, idem="one", account_ref=None, task="Research"):
    store, worker_id, key, _, ticks = service
    job = JobSubmission(idem, "codex", task, "research", "research",
                        datetime.fromtimestamp(ticks[0] + 600, timezone.utc), 60)
    return store.request("submit", Submission(worker_id, job, account_ref).to_dict(), key)


def test_refserver_accounts_replaces_the_advertised_set(service):
    store, worker_id, key, epoch, _ = service
    body = _accounts(("ref-a", "Main", True), ("ref-b", "Second", False))
    assert store.request("accounts", {"worker_epoch": epoch, "accounts": body}, key) == {"account_count": 2}
    assert store.advertised_accounts(worker_id) == body
    assert store.request("accounts", {"worker_epoch": epoch, "accounts": body[1:]}, key) == {"account_count": 1}
    assert store.advertised_accounts(worker_id) == body[1:]
    assert store.request("accounts", {"worker_epoch": epoch, "accounts": []}, key) == {"account_count": 0}
    assert store.advertised_accounts(worker_id) == []
    with pytest.raises(ProtocolError, match="stale_epoch"):
        store.request("accounts", {"worker_epoch": epoch + 1, "accounts": body}, key)
    for bad in ({"worker_epoch": epoch}, {"worker_epoch": epoch, "accounts": body, "extra": 1},
                {"worker_epoch": epoch, "accounts": _accounts(("a", "x", True), ("b", "y", True))}):
        with pytest.raises(ProtocolError, match="invalid_request"):
            store.request("accounts", bad, key)
    assert store.advertised_accounts(worker_id) == []  # refused requests change nothing


def test_refserver_accepts_only_an_advertised_account_ref(service):
    store, _, key, epoch, _ = service
    with pytest.raises(ProtocolError, match="invalid_request"):
        _service_submit(service, account_ref="ref-a")  # nothing advertised yet
    store.request("accounts", {"worker_epoch": epoch, "accounts": _accounts(("ref-a", "Main", True))}, key)
    with pytest.raises(ProtocolError, match="invalid_request"):
        _service_submit(service, account_ref="ref-z")
    job = _service_submit(service, account_ref="ref-a")
    plain = _service_submit(service, idem="plain")
    # account_ref is part of the normalized idempotency payload.
    assert _service_submit(service, account_ref="ref-a") == job
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        _service_submit(service)
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        _service_submit(service, idem="plain", account_ref="ref-a")
    # An identical replay still returns the job after the account is withdrawn.
    store.request("accounts", {"worker_epoch": epoch, "accounts": []}, key)
    assert _service_submit(service, account_ref="ref-a") == job
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert claim["job_id"] == job["job_id"] and claim["submission"]["account_ref"] == "ref-a"
    assert plain["state"] == "queued"


def test_refserver_registration_clears_accounts_and_gates_chosen_jobs(service):
    """A new registration forgets the advertised set; a job carrying account_ref
    waits until that registration sends `accounts`, while plain jobs still flow."""
    store, worker_id, key, epoch, _ = service
    store.request("accounts", {"worker_epoch": epoch, "accounts": _accounts(("ref-a", "Main", True))}, key)
    chosen = _service_submit(service, account_ref="ref-a")
    epoch = store.request("register", {}, key)["worker_epoch"]
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    assert store.advertised_accounts(worker_id) == []
    plain = _service_submit(service, idem="plain")
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert claim["job_id"] == plain["job_id"] and "account_ref" not in claim["submission"]
    store.request("reconcile", {"worker_epoch": epoch, "job_id": plain["job_id"], "epoch": claim["epoch"],
                                "state": "failed", "execution_stopped": False, "unlaunched": True}, key)
    assert store.request("poll", {"worker_epoch": epoch}, key)["claim"] is None
    store.request("accounts", {"worker_epoch": epoch, "accounts": _accounts(("ref-a", "Main", True))}, key)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert claim["job_id"] == chosen["job_id"] and claim["submission"]["account_ref"] == "ref-a"


def test_refserver_revocation_clears_advertised_accounts(service):
    store, worker_id, key, epoch, _ = service
    store.request("accounts", {"worker_epoch": epoch, "accounts": _accounts(("ref-a", "Main", True))}, key)
    store.revoke(worker_id)
    assert store.advertised_accounts(worker_id) == []


def test_refserver_unknown_operations_are_unsupported_version(service):
    store, _, key, _, _ = service
    with pytest.raises(ProtocolError) as refused:
        store.request("nonexistent", {}, key)
    assert (refused.value.code, refused.value.status) == ("unsupported_version", 404)


# --- client: sending accounts ---------------------------------------------------------


class _ChoiceAdapter(FakeAdapter):
    """Records the account the lease holds when the provider would start."""

    def __init__(self, root):
        super().__init__()
        self.root = root
        self.leased = []

    def start(self, job, workspace, *, worker_epoch):
        self.leased.append(AccountLeaseStore(self.root, "codex").current().account_identity)
        return super().start(job, workspace, worker_epoch=worker_epoch)


class _NoAccountsTransport(StoreTransport):
    """A backend without the extension: ``accounts`` is an unknown operation."""

    code = "unsupported_version"

    def request(self, operation, data):
        if operation == "accounts":
            self.calls.append(operation)
            raise ProtocolError(self.code, 404)
        return super().request(operation, data)


@pytest.fixture
def choice(root, tmp_path):
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    update_worker_settings(root, enabled=True)
    configure_worker_service(root, URL)
    cli.set_worker_account(root, "1")
    cli.allow_worker_account(root, "2", label="Second")
    adapter = _ChoiceAdapter(root)
    runtime = WorkerRuntime(root, adapter=adapter)  # the real pin and roster, no fixed identity
    transport = StoreTransport(store, paired["device_key"])
    remote = RemoteClient(runtime, URL, paired["device_key"], worker_id=paired["worker_id"], transport=transport)
    remote.tick()
    return remote, runtime, adapter, store, ticks, paired, transport, root


def _submit(choice, idem="one", account_ref=None):
    _, _, _, store, ticks, paired, _, _ = choice
    job = JobSubmission(idem, "codex", "Research", "research", "research",
                        datetime.fromtimestamp(ticks[0] + 100, timezone.utc), 60)
    return store.request("submit", Submission(paired["worker_id"], job, account_ref).to_dict(),
                         paired["device_key"])["job_id"]


def _sent(transport):
    return [data for op, data in transport.requests if op == "accounts"]


def test_registration_advertises_refs_labels_and_default_only(choice):
    remote, _, _, store, _, paired, transport, root = choice
    (body,) = _sent(transport)
    alice, bob = _entry(root, ALICE), _entry(root, BOB)
    assert body == {"worker_epoch": remote.worker_epoch, "accounts": [
        {"account_ref": alice.account_ref, "label": "work", "default": True},
        {"account_ref": bob.account_ref, "label": "Second", "default": False},
    ]}
    text = json.dumps(body)
    assert "codex:" not in text and "@" not in text and "acct-" not in text and SECRET not in text
    assert store.advertised_accounts(paired["worker_id"]) == body["accounts"]
    assert remote.accounts_offered() and remote.journal.accounts_advertised()


def test_no_resend_without_a_change_and_resend_on_each_change(choice):
    remote, _, _, store, _, paired, transport, root = choice
    for _ in range(3):
        remote.tick()
    assert len(_sent(transport)) == 1  # nothing changed: nothing is resent
    cli.label_worker_account(root, "2", "Renamed")
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 2 and _sent(transport)[-1]["accounts"][1]["label"] == "Renamed"
    cli.set_worker_account(root, "2")  # the default moved
    remote.tick()
    assert [e["default"] for e in _sent(transport)[-1]["accounts"]] == [False, True]
    cli.allow_worker_account(root, "5")
    remote.tick()
    assert len(_sent(transport)[-1]["accounts"]) == 3
    cli.disallow_worker_account(root, "5")
    remote.tick()
    assert len(_sent(transport)) == 5 and len(_sent(transport)[-1]["accounts"]) == 2
    # A new registration re-advertises the unchanged set.
    store.request("register", {}, paired["device_key"])
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 6 and _sent(transport)[-1]["worker_epoch"] == remote.worker_epoch
    assert store.advertised_accounts(paired["worker_id"]) == _sent(transport)[-1]["accounts"]


@pytest.mark.parametrize("code", ["unsupported_version", "not_found"])
def test_unsupported_backend_is_recorded_until_the_next_registration(choice, code):
    """Spec servers answer 404 `unsupported_version`; reference servers that
    predate the extension answer 404 `not_found`. Both turn the feature off."""
    remote, runtime, adapter, store, _, paired, _, root = choice
    transport = _NoAccountsTransport(store, paired["device_key"])
    transport.code = code
    client = RemoteClient(runtime, URL, paired["device_key"], worker_id="other-binding", transport=transport)
    client.tick()
    client.tick()
    assert transport.calls.count("accounts") == 1
    assert client.state == "online" and not client.accounts_offered()
    cli.label_worker_account(root, "2", "Changed")
    client.tick()
    assert transport.calls.count("accounts") == 1  # no resend to a backend without the extension
    # Heartbeats and claims carry on: a plain job still runs on the default pin.
    _submit(choice)
    client.tick()
    assert "poll" in transport.calls and runtime.reconcile_once().state == JobState.SUCCEEDED
    assert adapter.leased == [ALICE]
    store.request("register", {}, paired["device_key"])
    client.tick()
    client.tick()
    assert transport.calls.count("accounts") == 2  # discovery again after a new registration


def test_other_errors_retry_without_blocking_claims(choice):
    remote, runtime, _, _, _, _, transport, root = choice
    cli.label_worker_account(root, "2", "Retry me")
    transport.reject["accounts"] = ProtocolError("service_unavailable", 503)
    _submit(choice)
    remote.tick()
    assert remote.state == "online"
    assert runtime.store.queue()  # the claim was still taken in the same pass
    assert _sent(transport)[-1]["accounts"][1]["label"] == "Retry me"
    sent = len(_sent(transport))
    remote.tick()
    assert len(_sent(transport)) == sent + 1  # retried on the next pass, then acknowledged
    remote.tick()
    assert len(_sent(transport)) == sent + 1


def test_malformed_acknowledgement_is_retried(choice):
    remote, _, _, _, _, _, transport, root = choice
    original = transport.request

    def miscount(operation, data):
        value = original(operation, data)
        return {"account_count": 99} if operation == "accounts" else value

    transport.request = miscount
    cli.label_worker_account(root, "2", "X")
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 3  # never accepted as acknowledged
    transport.request = original
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 4


def test_a_claim_with_account_ref_is_malformed_unless_this_enrollment_advertised(choice):
    _, runtime, _, store, _, paired, _, root = choice
    transport = _NoAccountsTransport(store, paired["device_key"])
    client = RemoteClient(runtime, URL, paired["device_key"], worker_id="never-advertised", transport=transport)
    client.tick()
    # The service holds a choice the worker itself never advertised (another
    # registration did): this enrollment refuses it as a malformed claim.
    store.request("accounts", {"worker_epoch": client.worker_epoch,
                               "accounts": _accounts(("ref-x", "X", False))}, paired["device_key"])
    _submit(choice, account_ref="ref-x")
    client.tick()
    assert client.state == "offline" and client.journal.pending() == [] and not runtime.store.queue()


def test_the_advertised_flag_survives_a_new_client_for_the_same_enrollment(choice):
    remote, runtime, _, store, _, paired, _, root = choice
    cli.disallow_worker_account(root, "2")
    cli.set_worker_account(root, None)
    cli.disallow_worker_account(root, "1")  # nothing allowed now: an empty set is advertised
    remote.tick()
    assert not remote.accounts_offered()
    restarted = RemoteClient(runtime, URL, paired["device_key"], worker_id=paired["worker_id"],
                             transport=StoreTransport(store, paired["device_key"]))
    assert restarted._accounts_advertised is True


# --- runtime: resolving the claim's account before launch ------------------------------


def _run_job(choice):
    remote, runtime, *_ = choice
    remote.tick()
    result = runtime.reconcile_once()
    remote.tick()
    return result


def _reconciled(transport):
    return [(d["state"], d["execution_stopped"], d["unlaunched"]) for op, d in transport.requests if op == "reconcile"]


def test_an_absent_account_ref_runs_on_the_default(choice):
    _, runtime, adapter, store, _, paired, transport, _ = choice
    job_id = _submit(choice)
    result = _run_job(choice)
    assert result.state == JobState.SUCCEEDED and runtime.get(result.job_id).pinned_account_ref == ALICE
    assert adapter.leased == [ALICE]
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "succeeded"


def test_a_chosen_account_ref_runs_on_that_account(choice):
    _, runtime, adapter, store, _, paired, transport, root = choice
    job_id = _submit(choice, account_ref=_entry(root, BOB).account_ref)
    result = _run_job(choice)
    assert result.state == JobState.SUCCEEDED
    assert runtime.get(result.job_id).pinned_account_ref == BOB and adapter.leased == [BOB]
    assert load_worker_settings(root).pinned_account_ref == ALICE  # the default is unchanged
    assert _reconciled(transport) == [("succeeded", True, False)]
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "succeeded"


def test_a_ref_removed_after_the_claim_fails_unlaunched_without_substitution(choice):
    remote, runtime, adapter, store, _, paired, transport, root = choice
    job_id = _submit(choice, account_ref=_entry(root, BOB).account_ref)
    remote.tick()  # claimed and admitted locally
    assert runtime.store.queue()
    cli.disallow_worker_account(root, "2")
    result = runtime.reconcile_once()
    assert result.state == JobState.FAILED and result.diagnostic_code == "provider_auth_unavailable"
    assert adapter.starts == 0 and AccountLeaseStore(root, "codex").current() is None
    assert result.pinned_account_ref is None  # never substituted with the default
    remote.tick()
    assert _reconciled(transport) == [("failed", False, True)]
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "failed"


def test_a_relabelled_or_re_allowed_account_keeps_working(choice):
    _, runtime, adapter, _, _, _, _, root = choice
    ref = _entry(root, BOB).account_ref
    _submit(choice, account_ref=ref)
    cli.label_worker_account(root, ref, "New label")  # the reference is stable across relabels
    result = _run_job(choice)
    assert result.state == JobState.SUCCEEDED and adapter.leased == [BOB]


def test_no_default_and_no_choice_fails_unlaunched(choice):
    _, runtime, adapter, _, _, _, transport, root = choice
    cli.set_worker_account(root, None)
    _submit(choice)
    result = _run_job(choice)  # still claims: an allowlisted account is offered and present
    assert result.state == JobState.FAILED and result.diagnostic_code == "provider_unavailable"
    assert adapter.starts == 0 and _reconciled(transport) == [("failed", False, True)]


def test_no_default_still_runs_a_chosen_account(choice):
    _, runtime, adapter, _, _, _, _, root = choice
    cli.set_worker_account(root, None)
    _submit(choice, account_ref=_entry(root, ALICE).account_ref)
    assert _run_job(choice).state == JobState.SUCCEEDED and adapter.leased == [ALICE]


def test_withdrawing_every_account_still_reconciles_a_queued_choice(choice):
    """The owner clears the default and withdraws every allowlisted account
    after the service queued a job for one of them: the worker keeps polling,
    claims it and fails it before launch instead of leaving it to expire."""
    remote, runtime, adapter, store, _, paired, transport, root = choice
    job_id = _submit(choice, account_ref=_entry(root, BOB).account_ref)
    cli.set_worker_account(root, None)
    cli.disallow_worker_account(root, "1")
    cli.disallow_worker_account(root, "2")
    remote.tick()  # acknowledges the empty set, then polls
    assert _sent(transport)[-1]["accounts"] == [] and "poll" in transport.calls
    result = runtime.reconcile_once()
    assert result.state == JobState.FAILED and result.diagnostic_code == "provider_auth_unavailable"
    assert adapter.starts == 0 and result.pinned_account_ref is None
    remote.tick()
    assert _reconciled(transport) == [("failed", False, True)]
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "failed"


def test_an_enrollment_that_never_advertised_and_has_no_pin_claims_nothing(root, tmp_path):
    """Without a pin and without ever advertising, there is nothing to resolve
    or reconcile, so the worker does not poll."""
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    update_worker_settings(root, enabled=True)
    configure_worker_service(root, URL)
    runtime = WorkerRuntime(root, adapter=_ChoiceAdapter(root))
    transport = _NoAccountsTransport(store, paired["device_key"])
    remote = RemoteClient(runtime, URL, paired["device_key"], worker_id=paired["worker_id"], transport=transport)
    remote.tick()
    remote.tick()
    assert "poll" not in transport.calls and not runtime.store.queue()


def test_an_unreadable_choice_fails_closed(choice, monkeypatch):
    _, runtime, adapter, _, _, _, transport, root = choice
    _submit(choice, account_ref=_entry(root, BOB).account_ref)
    choice[0].tick()

    def broken(_job_id):
        raise LookupError("no claim")

    monkeypatch.setattr(runtime, "remote_account_ref", broken)
    result = runtime.reconcile_once()
    assert result.state == JobState.FAILED and result.diagnostic_code == "provider_auth_unavailable"
    assert adapter.starts == 0


def test_a_chosen_account_missing_from_the_roster_fails_before_any_lease(choice):
    _, runtime, adapter, _, _, _, transport, root = choice
    _submit(choice, account_ref=_entry(root, BOB).account_ref)
    choice[0].tick()
    _write_codex_roster(root, {"1": {"email": "alice@example.com", "accountId": "acct-alice", "alias": "work"}})
    result = runtime.reconcile_once()
    assert result.state == JobState.FAILED and result.diagnostic_code == "provider_auth_unavailable"
    assert adapter.starts == 0 and AccountLeaseStore(root, "codex").current() is None
    choice[0].tick()
    assert _reconciled(transport) == [("failed", False, True)]


# --- end to end over loopback ---------------------------------------------------------------


WAIT = 20


def test_loopback_allowlist_advertise_submit_test_and_run(root, keychain, capsys):  # noqa: F811
    store = ControlStore(root.parent / "service" / "db")
    with make_server(store, port=0) as server:
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            assert _run(root, "pair", url, store.issue_code()) == 0
            from openswap.worker.pairing import load_enrollment

            enrollment = load_enrollment(url)
            assert _run(root, "account", "1") == 0
            assert _run(root, "account", "allow", "2", "--label", "Second") == 0
            update_worker_settings(root, enabled=True)
            adapter = _ChoiceAdapter(root)
            runtime = WorkerRuntime(root, adapter=adapter)
            remote = RemoteClient(runtime, url, enrollment.device_key, worker_id=enrollment.worker_id)
            remote.tick()
            assert remote.state == "online" and remote.accounts_offered()
            bob = _entry(root, BOB)
            assert [e["account_ref"] for e in store.advertised_accounts(enrollment.worker_id)] == [
                _entry(root, ALICE).account_ref, bob.account_ref,
            ]
            capsys.readouterr()
            base = ["submit-test", "--url", url, "--task", "Research this synthetic topic",
                    "--workspace-id", "research", "--runtime-limit", "60", "--expires-in", "600",
                    "--i-understand-this-is-a-test-tool"]
            assert _run(root, *base, "--account-ref", "0" * 32) == 1
            refused = capsys.readouterr().err
            assert "invalid_request" in refused and "--account-ref=" + "0" * 32 in refused
            assert _run(root, *base, "--account-ref", bob.account_ref) == 0
            job_id = json.loads(capsys.readouterr().out)["job_id"]
            remote.tick()
            result = runtime.reconcile_once()
            assert result.state == JobState.SUCCEEDED and adapter.leased == [BOB]
            remote.tick()
            transport = Transport(url, enrollment.device_key)
            assert transport.request("job", {"job_id": job_id})["state"] == "succeeded"
        finally:
            server.shutdown()
            serving.join(WAIT)
    assert not serving.is_alive()



def test_a_claude_account_can_be_allowed_for_a_per_job_choice(root):
    carol = stable_account_identity("claude", "carol@example.com", "")
    entry = cli.allow_worker_account(root, "claude:4")
    assert entry.identity == carol and entry.label == "claudey"
    assert _entry(root, carol).account_ref == entry.account_ref
