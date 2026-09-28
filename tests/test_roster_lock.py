"""Roster mutators must take lock_file so a batched import cannot clobber them."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from openswap.exceptions import AccountNotFoundError
from openswap.switcher import ClaudeAccountSwitcher

from tests.conftest import patch_engine_filelock


class SpyLock:
    def __init__(self, path, timeout=10.0):
        self.path = path
        self.timeout = timeout

    def __enter__(self):
        entered.append(self.path)
        return self

    def __exit__(self, *exc):
        return False


entered: list[Path] = []


def _switcher_with_roster(temp_home: Path, sample_sequence_data: dict) -> ClaudeAccountSwitcher:
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher._write_json(switcher.sequence_file, sample_sequence_data)
    return switcher


class TestRosterWritersTakeAccountLock:
    def test_set_alias_holds_account_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        entered.clear()
        patch_engine_filelock(monkeypatch, SpyLock)
        switcher = _switcher_with_roster(temp_home, sample_sequence_data)

        switcher.set_alias("1", "dev")

        assert entered == [switcher.lock_file]
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["alias"] == "dev"

    def test_unset_alias_holds_account_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        sample_sequence_data["accounts"]["1"]["alias"] = "dev"
        entered.clear()
        patch_engine_filelock(monkeypatch, SpyLock)
        switcher = _switcher_with_roster(temp_home, sample_sequence_data)

        switcher.unset_alias("1")

        assert entered == [switcher.lock_file]
        data = switcher._get_sequence_data()
        assert "alias" not in data["accounts"]["1"]

    def test_remove_account_holds_account_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        entered.clear()
        monkeypatch.setattr("openswap.worker.leases.FileLock", SpyLock)
        switcher = _switcher_with_roster(temp_home, sample_sequence_data)

        switcher.remove_account("2", assume_yes=True)

        assert entered == [switcher.lock_file]
        data = switcher._get_sequence_data()
        assert "2" not in data["accounts"]

    def test_set_account_disabled_holds_account_lock(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        entered.clear()
        patch_engine_filelock(monkeypatch, SpyLock)
        switcher = _switcher_with_roster(temp_home, sample_sequence_data)

        switcher.set_account_disabled("2", True)

        assert entered == [switcher.lock_file]
        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["disabled"] is True

    def test_add_account_holds_account_lock(
        self, temp_home: Path, mock_claude_config: Path, monkeypatch
    ):
        entered.clear()
        patch_engine_filelock(monkeypatch, SpyLock)
        monkeypatch.setattr("openswap.worker.leases.FileLock", SpyLock)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        with patch.object(switcher, "_read_capture_credentials", return_value=creds), \
             patch("openswap.oauth.fetch_oauth_profile", return_value=None):
            switcher.add_account(assume_yes=True)

        assert entered
        assert all(path == switcher.lock_file for path in entered)
        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]

    def test_add_account_does_not_hold_lock_during_overwrite_prompt(
        self, temp_home: Path, mock_claude_config: Path, monkeypatch
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "other@example.com",
            "uuid": "u-other",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)

        depth = [0]

        class TrackingLock:
            def __init__(self, path, timeout=10.0):
                self.path = path

            def __enter__(self):
                depth[0] += 1
                return self

            def __exit__(self, *exc):
                depth[0] -= 1
                return False

        patch_engine_filelock(monkeypatch, TrackingLock)

        def prompt(*_a, **_k):
            assert depth[0] == 0
            return "y"

        monkeypatch.setattr("builtins.input", prompt)
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch.object(switcher, "_read_capture_credentials", return_value=creds):
            switcher.add_account(slot=1)

        assert depth[0] == 0
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "test@example.com"

    def test_add_account_inits_roster_under_lock(
        self, temp_home: Path, mock_claude_config: Path, monkeypatch
    ):
        switcher = ClaudeAccountSwitcher()
        depth = [0]
        init_under_lock = []

        class TrackingLock:
            def __init__(self, path, timeout=10.0):
                self.path = path

            def __enter__(self):
                depth[0] += 1
                entered.append(self.path)
                return self

            def __exit__(self, *exc):
                depth[0] -= 1
                return False

        patch_engine_filelock(monkeypatch, TrackingLock)
        real_init = switcher._init_sequence_file

        def init_while_locked():
            init_under_lock.append(depth[0] > 0)
            return real_init()

        monkeypatch.setattr(switcher, "_init_sequence_file", init_while_locked)
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch.object(switcher, "_read_capture_credentials", return_value=creds):
            switcher.add_account(assume_yes=True)

        assert init_under_lock == [True]

    def test_add_account_slot_move_keeps_source_if_dest_write_fails(
        self, temp_home: Path, mock_claude_config: Path, monkeypatch
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})

        with patch.object(switcher, "_read_capture_credentials", return_value=creds):
            switcher.add_account(assume_yes=True)

            def write_fails(account_num, email, credentials):
                raise OSError("disk full")

            monkeypatch.setattr(switcher, "_write_account_credentials", write_fails)
            with pytest.raises(OSError, match="disk full"):
                switcher.add_account(slot=5)

        data = switcher._get_sequence_data()
        assert "1" in data["accounts"]
        assert "5" not in data["accounts"]
        assert switcher._read_account_credentials("1", "test@example.com")

    def test_add_account_displace_same_email_keeps_dest_files(
        self, temp_home: Path, monkeypatch
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "test@example.com",
            "uuid": "u-org-a",
            "organizationUuid": "org-a",
            "organizationName": "A",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)
        switcher._write_account_credentials(
            "1", "test@example.com", json.dumps({"old": True})
        )
        cfg = {
            "oauthAccount": {
                "emailAddress": "test@example.com",
                "accountUuid": "u-org-b",
                "organizationUuid": "org-b",
                "organizationName": "B",
            }
        }
        (temp_home / ".claude.json").write_text(json.dumps(cfg))
        creds = json.dumps({"claudeAiOauth": {"accessToken": "new-tok"}})
        with patch.object(switcher, "_read_capture_credentials", return_value=creds):
            switcher.add_account(slot=1, assume_yes=True)

        stored = switcher._read_account_credentials("1", "test@example.com")
        assert stored
        assert "new-tok" in stored
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["organizationUuid"] == "org-b"

    def test_add_account_overwrite_succeeds_if_slot_vacated(
        self, temp_home: Path, mock_claude_config: Path, monkeypatch
    ):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["1"] = {
            "email": "other@example.com",
            "uuid": "u-other",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [1]
        switcher._write_json(switcher.sequence_file, data)

        def vacate_then_confirm(*_a, **_k):
            seq = switcher._get_sequence_data()
            seq["accounts"] = {}
            seq["sequence"] = []
            switcher._write_json(switcher.sequence_file, seq)
            return "y"

        monkeypatch.setattr("builtins.input", vacate_then_confirm)
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch.object(switcher, "_read_capture_credentials", return_value=creds):
            switcher.add_account(slot=1)

        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "test@example.com"

    def test_add_account_from_token_holds_account_lock(
        self, temp_home: Path, monkeypatch
    ):
        entered.clear()
        patch_engine_filelock(monkeypatch, SpyLock)
        monkeypatch.setattr("openswap.worker.leases.FileLock", SpyLock)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()

        switcher.add_account_from_token(
            "sk-ant-api03-test", email="key@example.com", assume_yes=True
        )

        assert entered
        assert all(path == switcher.lock_file for path in entered)
        data = switcher._get_sequence_data()
        assert data["accounts"]["1"]["email"] == "key@example.com"

    def test_remove_account_refuses_if_slot_identity_changed(
        self, temp_home: Path, sample_sequence_data: dict, monkeypatch
    ):
        switcher = _switcher_with_roster(temp_home, sample_sequence_data)

        def confirm_after_hijack(*_a, **_k):
            data = switcher._get_sequence_data()
            data["accounts"]["2"]["email"] = "hijacked@example.com"
            switcher._write_json(switcher.sequence_file, data)
            return "y"

        monkeypatch.setattr("builtins.input", confirm_after_hijack)
        with pytest.raises(AccountNotFoundError, match="no longer"):
            switcher.remove_account("2")

        data = switcher._get_sequence_data()
        assert data["accounts"]["2"]["email"] == "hijacked@example.com"
