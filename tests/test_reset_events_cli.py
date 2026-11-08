"""Tests for the `openswap events` subcommand."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from unittest.mock import patch

from openswap import cli, macos_keychain, paths
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
    _seed(replace(_webhook(), last_error="HTTP 500"))

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
    }


def test_test_command_exit_code_on_failure(temp_home, capsys, block_real_keychain, monkeypatch):
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


def test_test_command_reports_delivered(temp_home, capsys, block_real_keychain, monkeypatch):
    _seed(_webhook())
    block_real_keychain.data[(delivery.KEYCHAIN_SERVICE, "wh_1")] = "s3cret"
    monkeypatch.setattr(delivery, "post_once", lambda url, body, secret, *, now: None)

    code, out, _ = _run(["test", "wh_1"], capsys)

    assert code == 0
    assert out.strip() == "delivered"
