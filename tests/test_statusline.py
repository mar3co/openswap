"""Opt-in Claude Code status line: wrap the user's line, append the account name."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from openswap import statusline as sl
from openswap.exceptions import ConfigError
from openswap.models import Platform
from openswap.settings import _settings_write_lock, atomic_write_json, settings_path
from tests.test_codex_auth import _auth

_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")


def _backup_root(home: Path) -> Path:
    platform = Platform.detect()
    if platform in (Platform.LINUX, Platform.WSL):
        return home / ".local" / "share" / "openswap"
    if platform is Platform.MACOS:
        return home / "Library" / "Application Support" / "OpenSwap"
    return home / ".claude-swap-backup"


def _env(home: Path) -> dict[str, str]:
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.pop("XDG_DATA_HOME", None)
    env.pop("CODEX_HOME", None)
    return env


def _write_codex_live(home: Path, *, email: str, account_id: str) -> Path:
    codex = home / ".codex"
    codex.mkdir(exist_ok=True)
    path = codex / "auth.json"
    path.write_text(_auth(email=email, account_id=account_id), encoding="utf-8")
    return path


def _write_codex_roster(
    home: Path,
    *,
    email: str,
    account_id: str,
    alias: str = "",
    plan_type: str = "plus",
) -> Path:
    sequence = _backup_root(home) / "codex" / "sequence.json"
    sequence.parent.mkdir(parents=True, exist_ok=True)
    sequence.write_text(
        json.dumps(
            {
                "accounts": {
                    "1": {
                        "email": email,
                        "accountId": account_id,
                        "planType": plan_type,
                        "alias": alias,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return sequence


class TestAppendLabel:
    def test_empty_output_is_just_the_name(self):
        assert sl.append_label("", "work") == "work"

    def test_appends_on_the_line_with_percentages(self):
        assert sl.append_label("Opus  5h 23%\n", "work") == "Opus  5h 23% · work\n"

    def test_picks_the_percentage_row_on_multiline(self):
        text = "Opus  ~/proj\n5h 23%  7d 41%\n"
        assert sl.append_label(text, "Ads Online") == (
            "Opus  ~/proj\n5h 23%  7d 41% · Ads Online\n"
        )

    def test_no_percent_appends_last_nonempty_line(self):
        assert sl.append_label("hello\n", "work") == "hello · work\n"

    def test_blank_label_leaves_text_alone(self):
        assert sl.append_label("5h 23%\n", "") == "5h 23%\n"

    def test_does_not_duplicate_an_existing_name(self):
        assert sl.append_label("5h 23% · work\n", "work") == "5h 23% · work\n"

    def test_path_suffix_is_not_already_labeled(self):
        assert sl.append_label("Opus  ~/work\n", "work") == "Opus  ~/work · work\n"


class TestAccountLabel:
    def test_alias_wins(self):
        assert sl.account_label("user@x.com", alias="work", org_name="Acme") == "work"

    def test_org_name_when_no_alias(self):
        assert sl.account_label("user@x.com", alias="", org_name="Acme Corp") == "Acme Corp"

    def test_personal_when_managed_without_org(self):
        assert sl.account_label("user@x.com", alias="", org_name="") == "personal"

    def test_unmanaged_uses_email_local_part(self):
        assert sl.account_label("user@x.com", alias="", org_name="", managed=False) == "user"


class TestCurrentAccountLabel:
    def test_matches_email_and_org(self, tmp_path: Path):
        config = tmp_path / ".claude.json"
        config.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "user@example.com",
                        "organizationUuid": "org-a",
                    }
                }
            ),
            encoding="utf-8",
        )
        sequence = tmp_path / "sequence.json"
        sequence.write_text(
            json.dumps(
                {
                    "accounts": {
                        "1": {
                            "email": "user@example.com",
                            "organizationUuid": "org-a",
                            "organizationName": "Ads Online",
                            "alias": "",
                        },
                        "2": {
                            "email": "user@example.com",
                            "organizationUuid": "org-b",
                            "organizationName": "personal",
                            "alias": "home",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        assert sl.current_account_label(config, sequence) == "Ads Online"

    def test_alias_on_matching_slot(self, tmp_path: Path):
        config = tmp_path / ".claude.json"
        config.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "user@example.com",
                        "organizationUuid": "org-b",
                    }
                }
            ),
            encoding="utf-8",
        )
        sequence = tmp_path / "sequence.json"
        sequence.write_text(
            json.dumps(
                {
                    "accounts": {
                        "1": {
                            "email": "user@example.com",
                            "organizationUuid": "org-a",
                            "organizationName": "Ads Online",
                        },
                        "2": {
                            "email": "user@example.com",
                            "organizationUuid": "org-b",
                            "organizationName": "",
                            "alias": "home",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        assert sl.current_account_label(config, sequence) == "home"

    def test_missing_files_are_empty(self, tmp_path: Path):
        assert sl.current_account_label(tmp_path / "nope.json", tmp_path / "seq.json") == ""


class TestInstallWrap:
    def test_save_wrap_serializes_with_worker_settings_updates(self, tmp_path: Path, monkeypatch):
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        path = settings_path(backup)
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "worker": {"enabled": True, "paused": False},
                    "unrelated": {"keep": "value"},
                }
            ),
            encoding="utf-8",
        )

        lock_requested = threading.Event()
        errors: list[BaseException] = []
        real_lock_factory = _settings_write_lock

        def observed_lock(root: Path):
            lock_requested.set()
            return real_lock_factory(root)

        monkeypatch.setattr(sl, "_settings_write_lock", observed_lock)

        def save_statusline():
            try:
                sl.save_wrap(backup, inner_command="custom-line", created=True)
            except BaseException as error:  # propagate worker-thread failures
                errors.append(error)

        with real_lock_factory(backup):
            writer = threading.Thread(target=save_statusline)
            writer.start()
            assert lock_requested.wait(timeout=2)

            # Model the worker pause update while statusline waits for the same
            # settings lock. The statusline writer must reread after acquiring it.
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["worker"]["paused"] = True
            atomic_write_json(path, raw)

        writer.join(timeout=2)
        assert not writer.is_alive()
        assert errors == []
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["worker"]["paused"] is True
        assert saved["unrelated"] == {"keep": "value"}
        assert saved["statusline"] == {
            "innerCommand": "custom-line",
            "created": True,
        }

    def test_only_openswap_statusline_is_recognized_as_ours(self):
        assert sl.is_our_command("openswap statusline") is True
        assert sl.is_our_command("/usr/local/bin/openswap statusline") is True
        assert sl.is_our_command("cswap statusline") is False
        assert sl.is_our_command("/usr/local/bin/cswap statusline") is False
        assert sl._is_legacy_command("cswap statusline") is True
        assert sl._is_legacy_command("/usr/local/bin/cswap statusline") is True

    def test_install_migrates_legacy_command_without_nesting_it(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "statusLine": {
                        "type": "command",
                        "command": "/usr/local/bin/cswap statusline",
                        "padding": 2,
                    }
                }
            ),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.save_wrap(backup, inner_command="~/original.sh", created=False)

        result = sl.install(claude, backup, command="openswap statusline")

        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["statusLine"] == {
            "type": "command",
            "command": "openswap statusline",
            "padding": 2,
        }
        assert result == {"already": True, "created": False, "migrated": True}
        assert sl.load_wrap(backup)["innerCommand"] == "~/original.sh"

    def test_uninstall_cleans_up_legacy_command(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {"statusLine": {"type": "command", "command": "cswap statusline"}}
            ),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.save_wrap(backup, inner_command="~/original.sh", created=False)

        assert sl.uninstall(claude, backup)["restored"] is True
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["statusLine"]["command"] == "~/original.sh"

    def test_install_with_no_statusline_creates_ours(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        result = sl.install(claude, backup, command="openswap statusline")
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["statusLine"]["type"] == "command"
        assert settings["statusLine"]["command"] == "openswap statusline"
        assert result["created"] is True
        wrap = sl.load_wrap(backup)
        assert wrap["innerCommand"] is None
        assert wrap["created"] is True

    def test_install_wraps_existing_command(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "statusLine": {
                        "type": "command",
                        "command": "~/.claude/statusline.sh",
                        "padding": 2,
                    },
                }
            ),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.install(claude, backup, command="openswap statusline")
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["theme"] == "dark"
        assert settings["statusLine"]["command"] == "openswap statusline"
        assert settings["statusLine"]["padding"] == 2
        wrap = sl.load_wrap(backup)
        assert wrap["innerCommand"] == "~/.claude/statusline.sh"
        assert wrap["created"] is False

    def test_install_does_not_nest_our_command(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "statusLine": {
                        "type": "command",
                        "command": "openswap statusline",
                    }
                }
            ),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.save_wrap(backup, inner_command="~/old.sh", created=False)
        result = sl.install(claude, backup, command="openswap statusline")
        assert result["already"] is True
        wrap = sl.load_wrap(backup)
        assert wrap["innerCommand"] == "~/old.sh"

    def test_install_does_not_write_claude_json(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        login = tmp_path / ".claude.json"
        login.write_text('{"oauthAccount":{"emailAddress":"keep@x.com"}}', encoding="utf-8")
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.install(claude, backup, command="openswap statusline")
        assert json.loads(login.read_text(encoding="utf-8"))["oauthAccount"]["emailAddress"] == (
            "keep@x.com"
        )

    def test_uninstall_restores_inner(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        (claude / "settings.json").write_text(
            json.dumps(
                {
                    "statusLine": {
                        "type": "command",
                        "command": "jq -r .model.display_name",
                    }
                }
            ),
            encoding="utf-8",
        )
        sl.install(claude, backup, command="openswap statusline")
        sl.uninstall(claude, backup)
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["statusLine"]["command"] == "jq -r .model.display_name"
        assert sl.load_wrap(backup)["innerCommand"] is None

    def test_uninstall_removes_statusline_we_created(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.install(claude, backup, command="openswap statusline")
        sl.uninstall(claude, backup)
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert "statusLine" not in settings

    def test_install_refuses_torn_claude_settings(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        torn = claude / "settings.json"
        torn.write_text("{not json", encoding="utf-8")
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        with pytest.raises(ConfigError, match="overwrite"):
            sl.install(claude, backup, command="openswap statusline")
        assert torn.read_text(encoding="utf-8") == "{not json"

    def test_install_refuses_torn_openswap_settings(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        (backup / "settings.json").write_text("{nope", encoding="utf-8")
        with pytest.raises(ConfigError, match="overwrite"):
            sl.install(claude, backup, command="openswap statusline")
        assert not (claude / "settings.json").exists()
        assert (backup / "settings.json").read_text(encoding="utf-8") == "{nope"

    def test_legacy_migration_waits_until_wrap_state_is_readable(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        settings_file = claude / "settings.json"
        settings_file.write_text(
            json.dumps(
                {"statusLine": {"type": "command", "command": "cswap statusline"}}
            ),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        (backup / "settings.json").write_text("{nope", encoding="utf-8")

        with pytest.raises(ConfigError, match="overwrite"):
            sl.install(claude, backup, command="openswap statusline")

        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        assert settings["statusLine"]["command"] == "cswap statusline"

    def test_uninstall_refuses_torn_claude_settings(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.install(claude, backup, command="openswap statusline")
        torn = claude / "settings.json"
        torn.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConfigError, match="overwrite"):
            sl.uninstall(claude, backup)
        assert torn.read_text(encoding="utf-8") == "{not json"

    def test_install_writes_through_symlink(self, tmp_path: Path):
        repo = tmp_path / "dotfiles"
        repo.mkdir()
        tracked = repo / "claude-settings.json"
        tracked.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        claude = tmp_path / ".claude"
        claude.mkdir()
        link = claude / "settings.json"
        link.symlink_to(tracked)
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.install(claude, backup, command="openswap statusline")
        assert link.is_symlink(), "the dotfiles link must survive the write"
        data = json.loads(tracked.read_text(encoding="utf-8"))
        assert data["theme"] == "dark"
        assert data["statusLine"]["command"] == "openswap statusline"

    def test_save_wrap_refuses_torn_settings(self, tmp_path: Path):
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        torn = backup / "settings.json"
        torn.write_text("{nope", encoding="utf-8")
        with pytest.raises(ConfigError, match="overwrite"):
            sl.save_wrap(backup, inner_command="~/old.sh", created=False)
        assert torn.read_text(encoding="utf-8") == "{nope"

    def test_install_keeps_inner_if_claude_write_fails(self, tmp_path: Path, monkeypatch):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "~/.claude/statusline.sh"}}),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(sl, "_write_json", boom)
        with pytest.raises(OSError, match="disk full"):
            sl.install(claude, backup, command="openswap statusline")
        wrap = sl.load_wrap(backup)
        assert wrap["innerCommand"] == "~/.claude/statusline.sh"
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["statusLine"]["command"] == "~/.claude/statusline.sh"

    def test_install_migrates_legacy_backup_before_write(self, tmp_path: Path):
        from openswap.paths import LEGACY_BACKUP_DIRNAME

        backup = _backup_root(tmp_path)
        legacy = tmp_path / LEGACY_BACKUP_DIRNAME
        if backup.resolve() == legacy.resolve():
            pytest.skip("legacy and new backup roots coincide")
        legacy.mkdir()
        (legacy / "sequence.json").write_text(
            json.dumps({"accounts": {"1": {"email": "a@x.com", "organizationUuid": ""}}}),
            encoding="utf-8",
        )
        (tmp_path / ".claude").mkdir()
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "statusline", "--install"],
            capture_output=True,
            text=True,
            env=_env(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        assert not legacy.exists()
        assert (backup / "sequence.json").exists()
        wrap = sl.load_wrap(backup)
        assert wrap["created"] is True


class TestPaint:
    def test_wraps_inner_stdout_and_appends_name(self, tmp_path: Path):
        config = tmp_path / ".claude.json"
        config.write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "user@example.com",
                        "organizationUuid": "org-a",
                    }
                }
            ),
            encoding="utf-8",
        )
        sequence = tmp_path / "sequence.json"
        sequence.write_text(
            json.dumps(
                {
                    "accounts": {
                        "1": {
                            "email": "user@example.com",
                            "organizationUuid": "org-a",
                            "organizationName": "Ads Online",
                            "alias": "work",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        script = tmp_path / "inner.py"
        script.write_text(
            "import sys; sys.stdout.write('5h 23%  7d 41%\\n')\n",
            encoding="utf-8",
        )
        inner = subprocess.list2cmdline([sys.executable, str(script)])
        out = sl.paint(
            "{}",
            inner_command=inner,
            config_path=config,
            sequence_path=sequence,
        )
        assert out == "5h 23%  7d 41% · work\n"

    def test_no_inner_prints_just_the_name(self, tmp_path: Path):
        config = tmp_path / ".claude.json"
        config.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "user@example.com"}}),
            encoding="utf-8",
        )
        sequence = tmp_path / "sequence.json"
        sequence.write_text(
            json.dumps(
                {
                    "accounts": {
                        "1": {
                            "email": "user@example.com",
                            "organizationUuid": "",
                            "alias": "home",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert sl.paint("{}", inner_command=None, config_path=config, sequence_path=sequence) == (
            "home\n"
        )


class TestCLI:
    def test_help_lists_statusline(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_env(tmp_path),
        )
        assert result.returncode == 0
        assert "statusline" in result.stdout

    def test_statusline_help(self, tmp_path: Path):
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "statusline", "--help"],
            capture_output=True,
            text=True,
            env=_env(tmp_path),
        )
        assert result.returncode == 0
        assert "--install" in result.stdout
        assert "--uninstall" in result.stdout

    def test_paint_via_cli(self, tmp_path: Path):
        (tmp_path / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "user@example.com",
                        "organizationUuid": "",
                    }
                }
            ),
            encoding="utf-8",
        )
        backup = _backup_root(tmp_path)
        backup.mkdir(parents=True)
        (backup / "sequence.json").write_text(
            json.dumps(
                {
                    "accounts": {
                        "1": {
                            "email": "user@example.com",
                            "organizationUuid": "",
                            "alias": "home",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "statusline"],
            input="{}",
            capture_output=True,
            text=True,
            env=_env(tmp_path),
        )
        assert result.returncode == 0
        assert result.stdout == "home\n"


class TestCodexAccountLabel:
    def test_live_auth_and_roster_alias(self, temp_home: Path):
        auth = _write_codex_live(temp_home, email="user@example.com", account_id="acc-a")
        sequence = _write_codex_roster(
            temp_home,
            email="user@example.com",
            account_id="acc-a",
            alias="work",
        )
        assert sl.current_codex_account_label(auth, sequence) == "work"

    def test_unmanaged_uses_email_local_part(self, temp_home: Path):
        auth = _write_codex_live(temp_home, email="user@example.com", account_id="acc-live")
        sequence = _write_codex_roster(
            temp_home,
            email="other@example.com",
            account_id="acc-other",
            alias="work",
        )
        assert sl.current_codex_account_label(auth, sequence) == "user"

    def test_plan_type_when_no_alias(self, temp_home: Path):
        auth = _write_codex_live(temp_home, email="user@example.com", account_id="acc-a")
        sequence = _write_codex_roster(
            temp_home,
            email="user@example.com",
            account_id="acc-a",
            alias="",
            plan_type="plus",
        )
        assert sl.current_codex_account_label(auth, sequence) == "plus"

    def test_missing_files_are_empty(self, temp_home: Path):
        assert sl.current_codex_account_label(
            temp_home / ".codex" / "auth.json",
            _backup_root(temp_home) / "codex" / "sequence.json",
        ) == ""

    @staticmethod
    def _write_api_key_roster(home: Path) -> Path:
        sequence = _backup_root(home) / "codex" / "sequence.json"
        sequence.parent.mkdir(parents=True, exist_ok=True)
        sequence.write_text(
            json.dumps(
                {
                    "accounts": {
                        "1": {
                            "email": "",
                            "accountId": "",
                            "planType": "",
                            "kind": "api_key",
                            "alias": "first",
                        },
                        "2": {
                            "email": "",
                            "accountId": "",
                            "planType": "",
                            "kind": "api_key",
                            "alias": "second",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        return sequence

    @staticmethod
    def _write_slot_auth(sequence: Path, num: str, text: str) -> None:
        slot = sequence.parent / "slots" / num
        slot.mkdir(parents=True, exist_ok=True)
        (slot / "auth.json").write_text(text, encoding="utf-8")

    def test_api_key_matches_second_slot_by_actual_key(self, temp_home: Path):
        auth = temp_home / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text(
            json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-second"}),
            encoding="utf-8",
        )
        sequence = self._write_api_key_roster(temp_home)
        self._write_slot_auth(
            sequence, "1", json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-first"})
        )
        self._write_slot_auth(
            sequence, "2", json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-second"})
        )

        assert sl.current_codex_account_label(auth, sequence) == "second"

    def test_unmanaged_api_key_has_no_label(self, temp_home: Path):
        auth = temp_home / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text(
            json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-unmanaged"}),
            encoding="utf-8",
        )
        sequence = self._write_api_key_roster(temp_home)
        self._write_slot_auth(
            sequence, "1", json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-first"})
        )

        assert sl.current_codex_account_label(auth, sequence) == ""

    def test_api_key_match_ignores_json_formatting(self, temp_home: Path):
        auth = temp_home / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text(
            '{\n  "OPENAI_API_KEY": "sk-second",\n  "auth_mode": "apiKey"\n}\n',
            encoding="utf-8",
        )
        sequence = self._write_api_key_roster(temp_home)
        self._write_slot_auth(
            sequence, "2", '{"auth_mode":"apiKey","OPENAI_API_KEY":"sk-second"}'
        )

        assert sl.current_codex_account_label(auth, sequence) == "second"

    def test_api_key_missing_slot_file_does_not_match(self, temp_home: Path):
        auth = temp_home / ".codex" / "auth.json"
        auth.parent.mkdir()
        auth.write_text(
            json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-first"}),
            encoding="utf-8",
        )
        sequence = self._write_api_key_roster(temp_home)

        assert sl.current_codex_account_label(auth, sequence) == ""


class TestCodexCLI:
    def test_live_auth_and_roster_alias_prints_alias(self, temp_home: Path):
        _write_codex_live(temp_home, email="user@example.com", account_id="acc-a")
        _write_codex_roster(
            temp_home,
            email="user@example.com",
            account_id="acc-a",
            alias="work",
        )
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "statusline", "--codex"],
            capture_output=True,
            text=True,
            env=_env(temp_home),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "work\n"

    def test_unmanaged_prints_email_local_part(self, temp_home: Path):
        _write_codex_live(temp_home, email="user@example.com", account_id="acc-live")
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "statusline", "--codex"],
            capture_output=True,
            text=True,
            env=_env(temp_home),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "user\n"

    def test_codex_does_not_construct_engine(self, temp_home: Path, monkeypatch, capsys):
        _write_codex_live(temp_home, email="user@example.com", account_id="acc-a")
        _write_codex_roster(
            temp_home,
            email="user@example.com",
            account_id="acc-a",
            alias="work",
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        from openswap.cli import _statusline_command

        with (
            patch("openswap.cli.ClaudeAccountSwitcher") as claude_cls,
            patch("openswap.cli.Engine") as engine_cls,
            patch("openswap.codex.engine.CodexEngine") as codex_cls,
        ):
            assert _statusline_command(["--codex"]) == 0
            claude_cls.assert_not_called()
            engine_cls.assert_not_called()
            codex_cls.assert_not_called()
        assert capsys.readouterr().out == "work\n"

    def test_install_help_mentions_claude_settings_not_config_toml(self, temp_home: Path):
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "statusline", "--help"],
            capture_output=True,
            text=True,
            env=_env(temp_home),
        )
        assert result.returncode == 0
        help_text = result.stdout
        assert "--install" in help_text
        assert "Claude" in help_text
        assert "settings.json" in help_text
        assert "config.toml" not in help_text.lower()
        assert "--codex" in help_text


def test_the_app_pins_its_own_executable_and_recognises_that_hook(frozen_app, tmp_path):
    """From launchd argv[0] is `…/MacOS/OpenSwap`, and PATH may find uv first."""
    import shlex

    exe = frozen_app / "Contents" / "MacOS" / "OpenSwap"
    with patch.object(sl.sys, "argv", [str(exe)]), \
         patch.object(sl.shutil, "which", return_value="/Users/x/.local/bin/openswap"):
        command = sl.paint_command()
    assert command == f"{shlex.quote(str(exe))} statusline"
    assert sl.is_our_command(command)
    assert not sl.is_our_command("/Applications/Other.app/Contents/MacOS/Other statusline")


class TestSharedToggle:
    """`statusline --install/--uninstall` and the app's Settings toggle (plan 020)."""

    def test_set_installed_wraps_with_the_pinned_command_and_restores(self, tmp_path: Path, monkeypatch):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "~/.claude/statusline.sh"}}),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        monkeypatch.setattr(sl, "paint_command", lambda: "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline")
        assert sl.is_installed(claude) is False

        assert sl.set_installed(claude, backup, True) == {"already": False, "created": False, "migrated": False}
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["statusLine"]["command"] == "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline"
        assert sl.is_installed(claude) is True

        assert sl.set_installed(claude, backup, False) == {"restored": True}
        settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        assert settings["statusLine"]["command"] == "~/.claude/statusline.sh"
        assert sl.is_installed(claude) is False

    def test_a_missing_settings_file_is_not_installed(self, tmp_path: Path):
        assert sl.is_installed(tmp_path / ".claude") is False

    @pytest.mark.parametrize("flag, enabled", [("--install", True), ("--uninstall", False)])
    def test_the_cli_goes_through_the_shared_function(self, temp_home: Path, monkeypatch, capsys, flag, enabled):
        from openswap.cli import _statusline_command

        calls: list[tuple] = []

        def fake_set_installed(config_home, backup_root, on):
            calls.append((config_home, backup_root, on))
            return {"already": False, "created": True, "migrated": False} if on else {"restored": True}

        monkeypatch.setattr(sl, "set_installed", fake_set_installed)
        monkeypatch.setattr(sl, "install", lambda *a, **k: pytest.fail("call set_installed"))
        monkeypatch.setattr(sl, "uninstall", lambda *a, **k: pytest.fail("call set_installed"))
        assert _statusline_command([flag]) == 0
        from openswap import paths

        assert calls == [(paths.get_claude_config_home(), paths.get_backup_root(), enabled)]
        out = capsys.readouterr().out
        assert ("installed" if enabled else "restored") in out


class TestTemporaryCopy:
    """A translocated or mounted app must not pin its path (plan 020)."""

    def _setup(self, tmp_path: Path, monkeypatch):
        from openswap import bundle

        claude = tmp_path / ".claude"
        claude.mkdir()
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        monkeypatch.setattr(sl, "paint_command", lambda: "/Volumes/OpenSwap/OpenSwap.app/Contents/MacOS/OpenSwap statusline")
        monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
        return claude, backup

    def test_turning_on_is_refused_and_writes_nothing(self, tmp_path: Path, monkeypatch):
        from openswap.exceptions import ClaudeSwitchError

        claude, backup = self._setup(tmp_path, monkeypatch)
        with pytest.raises(ClaudeSwitchError, match="Move OpenSwap to Applications first."):
            sl.set_installed(claude, backup, True)
        assert not (claude / "settings.json").exists()

    def test_turning_off_still_works(self, tmp_path: Path, monkeypatch):
        claude, backup = self._setup(tmp_path, monkeypatch)
        sl.install(claude, backup, command="/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline")
        assert sl.set_installed(claude, backup, False) == {"restored": True}
        assert sl.is_installed(claude) is False

    def test_the_cli_reports_the_refusal(self, temp_home: Path, monkeypatch, capsys):
        from openswap import bundle
        from openswap.cli import _statusline_command

        monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
        assert _statusline_command(["--install"]) == 1
        assert "Move OpenSwap to Applications first." in capsys.readouterr().err
        from openswap import paths

        assert sl.is_installed(paths.get_claude_config_home()) is False



class TestAnotherOpenSwap:
    """A hook for another program is not this one, and turning it on repoints it
    (plan 020 step 6, revised)."""

    APP = "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap"

    def _setup(self, tmp_path: Path, monkeypatch, hook: str):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": hook, "padding": 1}}),
            encoding="utf-8",
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        sl.save_wrap(backup, inner_command="~/.claude/statusline.sh", created=False)
        monkeypatch.setattr(sl, "paint_command", lambda: f"{self.APP} statusline")
        return claude, backup

    @pytest.mark.parametrize(
        "hook",
        ["/Users/x/.local/bin/openswap statusline", "openswap statusline"],
        ids=["another-program", "bare"],
    )
    def test_is_not_this_program(self, tmp_path: Path, monkeypatch, hook):
        claude, _backup = self._setup(tmp_path, monkeypatch, hook)
        assert sl.is_installed(claude) is True
        assert sl.runs_this_program(claude) is False

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need extra privileges on Windows")
    def test_this_program_or_a_link_to_it(self, tmp_path: Path, monkeypatch):
        app = tmp_path / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
        app.parent.mkdir(parents=True)
        app.write_bytes(b"")
        link = tmp_path / "bin" / "openswap"
        link.parent.mkdir()
        link.symlink_to(app)
        claude, _backup = self._setup(tmp_path, monkeypatch, f"{link} statusline")
        monkeypatch.setattr(sl, "paint_command", lambda: f"{app} statusline")
        assert sl.runs_this_program(claude) is True

    def test_turning_on_repoints_and_keeps_the_wrap_and_the_prefix(self, tmp_path: Path, monkeypatch):
        claude, backup = self._setup(
            tmp_path, monkeypatch, "FOO=1 /Users/x/.local/bin/openswap statusline"
        )
        result = sl.set_installed(claude, backup, True)
        assert result == {"already": False, "created": False, "migrated": False, "repointed": True}
        block = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["statusLine"]
        assert block == {"type": "command", "command": f"FOO=1 {self.APP} statusline", "padding": 1}
        assert sl.load_wrap(backup) == {"innerCommand": "~/.claude/statusline.sh", "created": False}
        assert sl.runs_this_program(claude) is True
        # Then it is ours: a second call changes nothing.
        assert sl.set_installed(claude, backup, True)["already"] is True

    def test_a_bare_command_is_left_alone_when_nothing_better_can_be_pinned(self, tmp_path: Path, monkeypatch):
        claude, backup = self._setup(tmp_path, monkeypatch, "openswap statusline")
        monkeypatch.setattr(sl, "paint_command", lambda: sl.PAINT_COMMAND)
        assert sl.runs_this_program(claude) is True
        assert sl.set_installed(claude, backup, True)["already"] is True

    def test_the_cli_install_repoints_too(self, temp_home: Path, monkeypatch, capsys):
        from openswap import paths
        from openswap.cli import _statusline_command

        claude = paths.get_claude_config_home()
        claude.mkdir(parents=True, exist_ok=True)
        (claude / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "openswap statusline"}}),
            encoding="utf-8",
        )
        monkeypatch.setattr(sl, "paint_command", lambda: f"{self.APP} statusline")
        assert _statusline_command(["--install"]) == 0
        assert "now runs this OpenSwap" in capsys.readouterr().out
        block = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["statusLine"]
        assert block["command"] == f"{self.APP} statusline"


class TestRepointKeepsTheHookAsWritten:
    """Repointing replaces only the trailing `<program> statusline`: shell
    composition before it must stay shell syntax (Codex review on #109)."""

    APP = "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap"

    def _install_over(self, tmp_path: Path, monkeypatch, hook: str):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": hook}}), encoding="utf-8"
        )
        backup = tmp_path / "OpenSwap"
        backup.mkdir()
        monkeypatch.setattr(sl, "paint_command", lambda: f"{self.APP} statusline")
        return claude, backup

    def _command(self, claude: Path) -> str:
        return json.loads((claude / "settings.json").read_text(encoding="utf-8"))["statusLine"]["command"]

    @pytest.mark.parametrize(
        "prefix",
        [
            "echo ready && ",
            "cd ~/x; ",
            "FOO=1 BAR='a b' ",
            "  ",
        ],
        ids=["and", "semicolon", "quoted-env", "spaces"],
    )
    def test_the_prefix_is_kept_verbatim(self, tmp_path: Path, monkeypatch, prefix):
        claude, backup = self._install_over(tmp_path, monkeypatch, f"{prefix}/Users/x/.local/bin/openswap statusline")
        assert sl.set_installed(claude, backup, True)["repointed"] is True
        assert self._command(claude) == f"{prefix}{self.APP} statusline"

    def test_a_quoted_program_with_a_space_is_replaced_whole(self, tmp_path: Path, monkeypatch):
        claude, backup = self._install_over(
            tmp_path, monkeypatch, "echo hi && '/Users/x/My Tools/openswap' statusline  "
        )
        sl.set_installed(claude, backup, True)
        assert self._command(claude) == f"echo hi && {self.APP} statusline"

    def test_an_escaped_space_in_the_program_is_replaced_whole(self, tmp_path: Path, monkeypatch):
        claude, backup = self._install_over(tmp_path, monkeypatch, r"/Users/x/My\ Tools/openswap statusline")
        sl.set_installed(claude, backup, True)
        assert self._command(claude) == f"{self.APP} statusline"

    def test_a_hook_it_cannot_take_apart_is_left_alone_and_reported(self, tmp_path: Path, monkeypatch):
        hook = "/Users/x/.local/bin/openswap 'statusline'"
        claude, backup = self._install_over(tmp_path, monkeypatch, hook)
        assert sl.is_our_command(hook)
        with pytest.raises(ConfigError, match="cannot rewrite safely"):
            sl.set_installed(claude, backup, True)
        assert self._command(claude) == hook

    def test_the_move_from_a_uv_install_keeps_operators_too(self, tmp_path: Path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command",
                                       "command": "echo ready && /Users/x/.local/bin/openswap statusline"}}),
            encoding="utf-8",
        )
        import shlex

        program = Path(self.APP)
        assert sl.repoint_command(claude, program=program) is True
        assert self._command(claude) == f"echo ready && {shlex.quote(str(program))} statusline"
