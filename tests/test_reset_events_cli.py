"""Tests for the `openswap events` subcommand."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openswap import cli, macos_keychain, paths, reset_events
from openswap import reset_events_delivery as delivery
from openswap.reset_events import Destination, load_destinations, save_destinations

URL = "https://hooks.example.com/reset"


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    with patch("os.geteuid", return_value=1000, create=True), \
         patch.object(sys, "argv", ["openswap", "events", *argv]):
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _webhook(dest_id="wh_1", url=URL):
    return Destination(id=dest_id, kind="webhook", enabled=True, url=url)


def _seed(*dests):
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    save_destinations(root, [*load_destinations(root), *dests])
    return root


def test_add_webhook_prints_secret_once_and_saves(temp_home, capsys, block_real_keychain):
    code, out, _ = _run(
        ["add-webhook", URL, "--windows", "seven_day", "--events", "ending_soon,reset",
         "--lead-weekly", "120", "--min-left", "30"],
        capsys,
    )

    assert code == 0
    dests = load_destinations(paths.get_backup_root())
    added = next(d for d in dests if d.kind == "webhook")
    assert added.id.startswith("wh_") and len(added.id) == 11
    assert added.url == URL and added.enabled
    assert added.filter.windows == ("seven_day",)
    assert added.filter.events == ("window.ending_soon", "window.reset")
    assert added.filter.lead_s["seven_day"] == 7200
    assert added.filter.min_left_pct == 30.0
    secret = block_real_keychain.data[(delivery.KEYCHAIN_SERVICE, added.id)]
    assert out.count(secret) == 1
    assert added.id in out
    assert "Save this secret now; it can't be shown again." in out
    assert secret not in (paths.get_backup_root() / "reset_events.json").read_text()


def test_add_webhook_rejects_http_remote(temp_home, capsys, block_real_keychain):
    code, _, err = _run(["add-webhook", "http://hooks.example.com/reset"], capsys)

    assert code == 2
    assert "Webhook URL must use https:// unless the host is localhost." in err
    assert not any(d.kind == "webhook" for d in load_destinations(paths.get_backup_root()))
    assert not block_real_keychain.data


def test_add_webhook_leaves_no_secret_when_config_is_unreadable(
    temp_home, capsys, block_real_keychain,
):
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "reset_events.json").write_text("{broken")

    code, _, err = _run(["add-webhook", URL], capsys)

    assert code == 1
    assert "could not be read in full" in err
    assert not block_real_keychain.data
    assert (root / "reset_events.json").read_text() == "{broken"


def test_add_webhook_rolls_back_when_keychain_fails(temp_home, capsys, monkeypatch):
    def fail(dest_id):
        raise macos_keychain.KeychainError("locked")

    monkeypatch.setattr(delivery, "new_secret", fail)

    code, out, err = _run(["add-webhook", URL], capsys)

    assert code == 1
    assert "locked" in err
    assert "Secret:" not in out
    assert [d.id for d in load_destinations(paths.get_backup_root())] == ["mac"]
    assert not (paths.get_backup_root() / "reset_events.json").exists()


def _fail_saving(monkeypatch):
    def fail(root, fn):
        raise OSError("disk full")

    monkeypatch.setattr(reset_events, "update_destinations", fail)


def test_add_webhook_deletes_the_secret_when_saving_fails(
    temp_home, capsys, block_real_keychain, monkeypatch,
):
    created = []
    real_new_secret = delivery.new_secret
    monkeypatch.setattr(delivery, "new_secret", lambda dest_id: created.append(dest_id) or real_new_secret(dest_id))
    _fail_saving(monkeypatch)

    code, out, err = _run(["add-webhook", URL], capsys)

    assert code == 1
    assert len(created) == 1
    assert "disk full" in err
    assert "Secret:" not in out
    assert not block_real_keychain.data


def test_add_webhook_warns_when_secret_cleanup_also_fails(
    temp_home, capsys, block_real_keychain, monkeypatch,
):
    def locked(dest_id):
        raise macos_keychain.KeychainError("locked")

    monkeypatch.setattr(delivery, "delete_secret", locked)
    _fail_saving(monkeypatch)

    code, out, err = _run(["add-webhook", URL], capsys)

    assert code == 1
    assert "disk full" in err
    assert "its Keychain secret could not be deleted: locked" in out + err


def test_remove_deletes_secret(temp_home, capsys, block_real_keychain):
    root = _seed(_webhook())
    block_real_keychain.data[(delivery.KEYCHAIN_SERVICE, "wh_1")] = "s3cret"

    code, _, _ = _run(["remove", "wh_1"], capsys)

    assert code == 0
    assert [d.id for d in load_destinations(root)] == ["mac"]
    assert (delivery.KEYCHAIN_SERVICE, "wh_1") not in block_real_keychain.data


def test_remove_mac_refused(temp_home, capsys):
    root = _seed()

    code, _, err = _run(["remove", "mac"], capsys)

    assert code == 1
    assert "use the Mac toggle in Settings" in err
    assert [d.id for d in load_destinations(root)] == ["mac"]


def test_set_updates_mac_filter(temp_home, capsys):
    root = _seed()

    code, _, _ = _run(["set", "mac", "--windows", "five_hour", "--lead-5h", "15", "--enable"], capsys)

    assert code == 0
    mac = next(d for d in load_destinations(root) if d.id == "mac")
    assert mac.enabled
    assert mac.filter.windows == ("five_hour",)
    assert mac.filter.lead_s == {"five_hour": 900, "seven_day": 10800}


def test_list_json_shape(temp_home, capsys):
    _seed(replace(_webhook(), last_error="HTTP 500", last_failure="2026-11-09T15:00:04Z HTTP 500"))

    code, out, _ = _run(["list", "--json"], capsys)

    assert code == 0
    data = json.loads(out)
    assert data["schemaVersion"] == 1
    assert [d["id"] for d in data["destinations"]] == ["mac", "wh_1"]
    hook = data["destinations"][1]
    assert hook == {
        "id": "wh_1",
        "kind": "webhook",
        "enabled": True,
        "url": URL,
        "filter": {
            "accounts": [],
            "windows": ["five_hour", "seven_day"],
            "events": ["window.started", "window.ending_soon", "window.reset"],
            "leadS": {"five_hour": 1800, "seven_day": 10800},
            "minLeftPct": 20.0,
        },
        "lastError": "HTTP 500",
        "lastFailure": "2026-11-09T15:00:04Z HTTP 500",
    }


def test_list_shows_the_last_failure_after_a_later_success(temp_home, capsys):
    _seed(replace(_webhook(), last_failure="2026-11-09T15:00:04Z HTTP 500"))

    code, out, err = _run(["list"], capsys)

    assert code == 0
    assert "last failure: 2026-11-09T15:00:04Z HTTP 500" in out + err
    assert "last error" not in out + err


@pytest.fixture
def test_mac_name(monkeypatch):
    monkeypatch.setattr("openswap.worker.connect.computer_name", lambda: "Test Mac")


def test_test_command_exit_code_on_failure(
    temp_home, capsys, block_real_keychain, monkeypatch, test_mac_name,
):
    _seed(_webhook())
    block_real_keychain.data[(delivery.KEYCHAIN_SERVICE, "wh_1")] = "s3cret"
    sent = []

    def fail(url, body, secret, *, now):
        sent.append(json.loads(body))
        raise delivery.DeliveryError("HTTP 500")

    monkeypatch.setattr(delivery, "post_once", fail)

    code, out, err = _run(["test", "wh_1"], capsys)

    assert code == 1
    assert "HTTP 500" in err
    assert "delivered" not in out
    [body] = sent
    assert body["test"] is True
    assert body["type"] == "window.ending_soon"
    assert body["account"] == {"label": "Sample", "email": "sample@example.com", "org": ""}
    assert body["mac"] == "Test Mac"


def test_test_command_reports_delivered(
    temp_home, capsys, block_real_keychain, monkeypatch, test_mac_name,
):
    _seed(_webhook())
    block_real_keychain.data[(delivery.KEYCHAIN_SERVICE, "wh_1")] = "s3cret"
    monkeypatch.setattr(delivery, "post_once", lambda url, body, secret, *, now: None)

    code, out, _ = _run(["test", "wh_1"], capsys)

    assert code == 0
    assert out.strip() == "delivered"


def test_list_reports_an_unreadable_config(temp_home, capsys):
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "reset_events.json").write_text("{broken")

    code, out, err = _run(["list"], capsys)
    assert code == 1
    assert "could not be read in full" in err and "mac" in out

    code, out, err = _run(["list", "--json"], capsys)
    assert code == 1
    data = json.loads(out)
    assert "could not be read in full" in data["problem"]
    assert [d["id"] for d in data["destinations"]] == ["mac"]
    assert "could not be read in full" in err


def test_list_json_has_no_problem_when_config_is_fine(temp_home, capsys):
    _seed()

    code, out, _ = _run(["list", "--json"], capsys)

    assert code == 0
    assert "problem" not in json.loads(out)


def test_test_command_reports_an_unreadable_config(temp_home, capsys):
    root = paths.get_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "reset_events.json").write_text("{broken")

    code, _, err = _run(["test", "wh_1"], capsys)

    assert code == 1
    assert "could not be read in full" in err
    assert "No reset events destination" not in err


@pytest.mark.parametrize("flag", ["--lead-5h", "--lead-weekly"])
def test_lead_time_must_be_at_least_one_minute(temp_home, capsys, flag):
    root = _seed()

    code, _, err = _run(["set", "mac", flag, "0"], capsys)

    assert code == 2
    assert "1 or more" in err
    assert load_destinations(root)[0].filter.lead_s == {"five_hour": 1800, "seven_day": 10800}


def test_unknown_account_keys_warn_but_are_saved(temp_home, capsys, monkeypatch):
    root = _seed()
    known = SimpleNamespace(provider="claude", email="a@example.com", org_uuid="org-1")
    monkeypatch.setattr(cli, "_known_accounts", lambda switcher, debug: [known])

    code, _, err = _run(
        ["set", "mac", "--accounts", "claude:a@example.com:org-1,claude:typo@example.com:org-1"], capsys,
    )

    assert code == 0
    assert err.strip() == (
        "Warning: no current account matches claude:typo@example.com:org-1"
        " (known: claude:a@example.com:org-1)"
    )
    assert load_destinations(root)[0].filter.accounts == (
        "claude:a@example.com:org-1", "claude:typo@example.com:org-1")

    code, _, err = _run(["set", "mac", "--accounts", "claude:a@example.com:org-1"], capsys)
    assert code == 0 and err == ""


def test_set_without_changes_is_an_error(temp_home, capsys):
    root = _seed()
    config = root / "reset_events.json"
    before = config.read_text()

    code, _, err = _run(["set", "mac"], capsys)

    assert code == 2
    assert "Nothing to change" in err
    assert config.read_text() == before


@pytest.mark.parametrize("flag", ["--lead-5h", "--lead-weekly"])
def test_lead_time_is_capped_at_what_the_config_accepts(temp_home, capsys, flag):
    root = _seed()

    code, _, err = _run(["set", "mac", flag, str(reset_events.MAX_LEAD_S // 60 + 1)], capsys)
    assert code == 2
    assert "43200" in err

    code, _, _ = _run(["set", "mac", flag, str(reset_events.MAX_LEAD_S // 60)], capsys)
    assert code == 0
    assert reset_events.load_destinations_checked(root)[1] is None
