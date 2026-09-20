"""Detection of a ``~/.claude.json`` identity rewritten outside a switch (#46)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from openswap.credentials import ActiveCredentials
from openswap.engine.identity import IdentityDrift, credential_owner_slots
from openswap.menubar_display import (
    IDENTITY_ATTEMPTS_PER_HOUR,
    IDENTITY_RETRY_S,
    IDENTITY_SETTLE_S,
    IdentityDriftWatch,
    format_identity_drift_log,
)
from openswap.process_detection import ClaudeSession

from tests.test_engine_abi import EMAIL, PERSONAL_ORG, _two_org_engine

OBSERVED = IdentityDrift("1", "2", "observed", (EMAIL, PERSONAL_ORG))
INDETERMINATE = IdentityDrift(None, None, "indeterminate")
UNATTRIBUTED = IdentityDrift("1", None, "unattributed", (EMAIL, PERSONAL_ORG))


@pytest.mark.parametrize(
    ("live_fp", "backup_fps", "expected"),
    [
        ("fp-2", {"1": "fp-1", "2": "fp-2"}, ["2"]),
        ("fp-9", {"1": "fp-1", "2": "fp-2"}, []),
        ("fp-1", {"1": "fp-1", "2": "fp-1"}, ["1", "2"]),
        (None, {"1": None, "2": "fp-2"}, []),
    ],
)
def test_credential_owner_slots(live_fp, backup_fps, expected) -> None:
    assert credential_owner_slots(live_fp, backup_fps) == expected


def _set_live_credential(temp_home: Path, refresh_token: str) -> None:
    (temp_home / ".claude" / ".credentials.json").write_text(
        json.dumps({
            "claudeAiOauth": {
                "accessToken": "at-rotated",
                "refreshToken": refresh_token,
            }
        }),
        encoding="utf-8",
    )


def test_config_naming_another_slot_than_the_credential_owner_is_observed(
    temp_home: Path,
) -> None:
    s = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    before = (temp_home / ".claude.json").read_bytes()

    drift = s.identity_drift()

    assert drift == OBSERVED
    assert drift.config_identity == (EMAIL, PERSONAL_ORG)
    assert (temp_home / ".claude.json").read_bytes() == before


def test_unmanaged_config_identity_is_carried_exactly(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    (temp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "y@example.com"}}),
        encoding="utf-8",
    )

    drift = s.identity_drift()

    assert drift == IdentityDrift(None, "2", "observed")
    assert drift.config_identity == ("y@example.com", "")


def test_consistent_identity_is_no_drift(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-1")

    assert s.identity_drift() is None


def test_managed_config_with_an_unattributable_credential_is_not_consistent(
    temp_home: Path,
) -> None:
    drift = _two_org_engine(temp_home).identity_drift()

    assert drift == IdentityDrift("1", None, "unattributed")


def test_unmanaged_login_with_its_own_credential_is_no_drift(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    (temp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "y@example.com"}}),
        encoding="utf-8",
    )

    assert s.identity_drift() is None


def _unmanaged_config(temp_home: Path) -> None:
    (temp_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "y@example.com"}}),
        encoding="utf-8",
    )


def test_unmanaged_login_with_no_credential_is_not_consistent(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    _unmanaged_config(temp_home)
    (temp_home / ".claude" / ".credentials.json").unlink()

    assert s.identity_drift() == IdentityDrift(None, None, "unattributed")


def _share_slot_2_lineage_with_slot_1(s) -> None:
    s._write_account_credentials("1", EMAIL, s._read_account_credentials("2", EMAIL))


def test_config_naming_one_of_several_owners_is_consistent(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    _share_slot_2_lineage_with_slot_1(s)
    _set_live_credential(temp_home, "rt-2")

    assert s.identity_drift() is None


def test_config_naming_none_of_several_owners_is_a_drift(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    _unmanaged_config(temp_home)
    _share_slot_2_lineage_with_slot_1(s)
    _set_live_credential(temp_home, "rt-2")

    assert s.identity_drift() == IdentityDrift(None, "1's or Account-2", "observed")


def test_torn_config_is_indeterminate_not_logged_out(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    (temp_home / ".claude.json").write_text('{"oauthAccount": {"emai', encoding="utf-8")

    assert s.identity_drift() == INDETERMINATE


def test_torn_roster_is_indeterminate(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    s.sequence_file.write_text('{"accounts": {"1"', encoding="utf-8")

    assert s.identity_drift() == INDETERMINATE


def test_absent_config_is_no_drift(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    (temp_home / ".claude.json").unlink()

    assert s.identity_drift() is None


def test_degraded_live_read_is_indeterminate(temp_home: Path, monkeypatch) -> None:
    s = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    stale = (temp_home / ".claude" / ".credentials.json").read_text(encoding="utf-8")
    monkeypatch.setattr(
        s._store,
        "_read_active_credentials",
        lambda: ActiveCredentials(stale, False, degraded=True),
    )

    assert s.identity_drift() == INDETERMINATE


def test_unreadable_backup_is_indeterminate(temp_home: Path, monkeypatch) -> None:
    s = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    real = s._store._read_account_credentials_ex
    monkeypatch.setattr(
        s._store,
        "_read_account_credentials_ex",
        lambda num, email: ("", True) if num == "1" else real(num, email),
    )

    assert s.identity_drift() == INDETERMINATE


A = ("a@example.com", "")
B = ("b@example.com", "")


def test_watch_reports_a_drift_only_once_it_outlives_the_settle_window() -> None:
    watch = IdentityDriftWatch()
    assert watch.begin(0.0) == "run"
    assert watch.record(0.0, A, OBSERVED, "first sighting") is None

    assert watch.ready(IDENTITY_SETTLE_S - 1) is False
    assert watch.begin(IDENTITY_SETTLE_S) == "run"
    line = watch.record(IDENTITY_SETTLE_S, A, OBSERVED, "second sighting")

    assert line == "first sighting"
    assert watch.ready(IDENTITY_SETTLE_S + 3600) is False


def test_watch_drops_a_drift_that_did_not_settle() -> None:
    watch = IdentityDriftWatch()
    watch.record(0.0, A, OBSERVED, "mid-switch")
    assert watch.record(5.0, B, None, None) is None

    assert watch.ready(IDENTITY_SETTLE_S) is False


def test_watch_reports_the_same_drift_again_when_it_recurs() -> None:
    watch = IdentityDriftWatch()
    watch.record(0.0, A, OBSERVED, "first incident")
    assert watch.record(IDENTITY_SETTLE_S, A, OBSERVED, "x") == "first incident"

    assert watch.slot_seen(None, 100.0) is True
    assert watch.record(100.0, None, OBSERVED, "second incident") is None
    assert (
        watch.record(100.0 + IDENTITY_SETTLE_S, None, OBSERVED, "y")
        == "second incident"
    )


def test_watch_restarts_the_window_when_the_drift_changes() -> None:
    watch = IdentityDriftWatch()
    other = IdentityDrift("1", "3", "observed", (EMAIL, PERSONAL_ORG))
    watch.record(0.0, A, OBSERVED, "a")

    assert watch.record(IDENTITY_SETTLE_S, A, other, "b") is None
    assert watch.record(2 * IDENTITY_SETTLE_S, A, other, "c") == "b"


def test_watch_keeps_a_pending_drift_through_an_unattributable_pass() -> None:
    watch = IdentityDriftWatch()
    watch.record(0.0, A, OBSERVED, "first sighting")

    assert watch.record(IDENTITY_SETTLE_S, A, UNATTRIBUTED, "unused") is None
    assert watch.ready(IDENTITY_SETTLE_S + IDENTITY_RETRY_S) is True
    assert (
        watch.record(IDENTITY_SETTLE_S + IDENTITY_RETRY_S, A, OBSERVED, "x")
        == "first sighting"
    )


def test_steady_retries_leave_budget_for_a_real_incident() -> None:
    watch = IdentityDriftWatch()
    now = 0.0
    while now < 2 * 3600:
        assert watch.begin(now) == "run"
        watch.record(now, A, UNATTRIBUTED, None)
        now += IDENTITY_RETRY_S

    assert watch.slot_seen(B, now - 1) is True
    assert watch.begin(now) == "run"


def test_watch_disarms_until_the_live_identity_moves() -> None:
    watch = IdentityDriftWatch()
    watch.record(0.0, A, None, None)

    assert watch.ready(1.0) is False
    assert watch.slot_seen(A, 1.0) is False
    assert watch.slot_seen(B, 1.0) is True
    assert watch.begin(2.0) == "run"


def test_watch_charges_every_pass_and_resumes_when_the_window_rolls() -> None:
    watch = IdentityDriftWatch()
    for i in range(IDENTITY_ATTEMPTS_PER_HOUR):
        now = float(i)
        assert watch.slot_seen((f"{i}@example.com", ""), now) is True
        assert watch.begin(now) == "run"
        watch.record(now, (f"{i}@example.com", ""), None, None)

    assert watch.slot_seen(A, 100.0) is True
    assert watch.ready(100.0) is False
    assert watch.begin(100.0) == "exhausted"
    assert watch.begin(101.0) == "wait"
    assert watch.ready(3599.0) is False
    assert watch.begin(3600.0) == "run"


def test_an_identity_move_does_not_wait_out_an_older_backoff() -> None:
    watch = IdentityDriftWatch()
    watch.record(0.0, A, INDETERMINATE, "could not check")
    assert watch.ready(1.0) is False

    assert watch.slot_seen(B, 2.0) is True
    assert watch.begin(2.0) == "run"


def test_watch_says_it_cannot_check_again_after_a_report() -> None:
    watch = IdentityDriftWatch()
    assert watch.record(0.0, A, INDETERMINATE, "cannot check") == "cannot check"
    watch.record(300.0, A, OBSERVED, "drift")
    assert watch.record(300.0 + IDENTITY_SETTLE_S, A, OBSERVED, "x") == "drift"

    assert watch.record(900.0, A, INDETERMINATE, "cannot check 2") == "cannot check 2"


def test_watch_failure_is_backed_off_and_not_rearmed_by_config_writes() -> None:
    watch = IdentityDriftWatch()
    watch.record(0.0, A, None, None)
    assert watch.slot_seen(B, 10.0) is True

    watch.failed(10.0)

    assert watch.slot_seen(B, 11.0) is False
    assert watch.ready(11.0) is False
    assert watch.begin(10.0 + IDENTITY_RETRY_S) == "run"


def test_drift_log_names_the_sessions_alive_at_the_flip() -> None:
    started = datetime(2026, 9, 18, 22, 30).timestamp()
    line = format_identity_drift_log(
        OBSERVED,
        [ClaudeSession(801, "sid", "/repo", int(started * 1000), "interactive", "claude-desktop")],
        0,
        datetime(2026, 9, 19, 6, 30, 16).timestamp(),
    )

    assert "config names Account-1 but the live credential is Account-2's" in line
    assert "Config written 2026-09-19T06:30:16" in line
    assert "pid 801 claude-desktop interactive since 2026-09-18T22:30:00" in line


def test_drift_log_never_reports_unknown_as_none() -> None:
    unmanaged = IdentityDrift(None, "2", "observed", ("y@example.com", ""))
    bad_start = ClaudeSession(7, "sid", "/repo", "not-a-number", "bg", "cli")
    no_start = ClaudeSession(8, "sid", "/repo", 0, "bg", "cli")

    assert "running: could not enumerate" in format_identity_drift_log(
        unmanaged, None, 0, None
    )
    line = format_identity_drift_log(unmanaged, [bad_start, no_start], 2, None)
    assert "pid 7 cli bg since unknown" in line
    assert "pid 8 cli bg since unknown" in line
    assert "(+2 unreadable)" in line
    assert "config names an unmanaged account" in line
    assert "Config written unknown" in line
    assert "could not be checked" in format_identity_drift_log(
        INDETERMINATE, [], 0, None
    )


def test_drift_log_says_when_the_config_was_rewritten_during_the_check() -> None:
    before = datetime(2026, 9, 19, 6, 30, 16).timestamp()
    after = datetime(2026, 9, 19, 6, 30, 19).timestamp()

    steady = format_identity_drift_log(OBSERVED, [], 0, before, before)
    moved = format_identity_drift_log(OBSERVED, [], 0, before, after)

    assert "during the check" not in steady
    assert "Config written unknown (and again at 2026-09-19T06:30:19" in (
        format_identity_drift_log(OBSERVED, [], 0, None, after)
    )
    assert (
        "Config written 2026-09-19T06:30:16 (and again at 2026-09-19T06:30:19, "
        "during the check)" in moved
    )
