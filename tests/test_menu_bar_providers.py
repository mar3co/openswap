"""Provider selection and honest source labels for the native status title."""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import menubar


@pytest.fixture
def snapshot():
    claude = {"five_hour": {"pct": 42}, "seven_day": {"pct": 18}}
    codex = {"five_hour": {"pct": 13}, "seven_day": {"pct": 27}}
    return {
        "active_email": "claude@example.test", "active_alias": "Personal",
        "active_usage": claude, "active_last_good": claude,
        "codex_active_num": "2",
        "accounts": [
            ("1", "claude@example.test", True, claude, claude, "Personal", "", False, 100),
            ("codex:1", "other@example.test", False, {}, None, "Other", "", False, None),
            ("codex:2", "codex@example.test", True, codex, codex, "Work", "Plus", False, 100),
        ],
    }


def title(snapshot, provider, **options):
    return menubar.format_menu_bar_title(
        snapshot, menubar.MenuBarSettings(menu_bar_provider=provider, **options), now=200
    )


def test_provider_titles_identify_account_and_quota_source(snapshot):
    claude = title(snapshot, "claude")
    chatgpt = title(snapshot, "chatgpt")
    both = title(snapshot, "both")
    assert claude.startswith("Claude · Personal")
    assert "42%" in claude and "18%" in claude and "ChatGPT" not in claude
    assert chatgpt.startswith("ChatGPT (unverified) · Work")
    assert "Codex" in chatgpt and "13%" in chatgpt and "27%" in chatgpt
    assert "Personal" not in chatgpt and "Other" not in chatgpt
    assert claude in both and chatgpt in both
    assert title(snapshot, "logo") == ""


def test_hidden_names_keep_provider_and_usage_source(snapshot):
    result = title(snapshot, "both", show_account_name=False)
    assert "Claude" in result and "ChatGPT" in result and "Codex" in result
    assert "Personal" not in result and "Work" not in result
    assert "42%" in result and "13%" in result


def test_chatgpt_scoped_limits_never_leak_from_claude(snapshot):
    snapshot["accounts"][2][3]["scoped"] = [{"name": "Fable", "pct": 99}]
    result = title(snapshot, "chatgpt", title_scoped=True, title_pct="off")
    assert "Work" in result and "unverified" in result
    assert "%" not in result and "Fable" not in result


@pytest.mark.parametrize("active_num", [None, "9"])
def test_missing_shared_login_never_guesses_saved_account(snapshot, active_num):
    snapshot["codex_active_num"] = active_num
    result = title(snapshot, "chatgpt")
    assert result == "ChatGPT · No shared account"
    assert "Work" not in result and "Other" not in result and "%" not in result


def test_empty_provider_states_remain_distinguishable():
    result = title({}, "both")
    assert "Claude · No account" in result
    assert "ChatGPT · No shared account" in result
    assert title({}, "logo") == ""


def test_chatgpt_unavailable_quota_does_not_become_zero(snapshot):
    row = list(snapshot["accounts"][2])
    row[3:5] = ["unavailable", None]
    snapshot["accounts"][2] = tuple(row)
    result = title(snapshot, "chatgpt")
    assert "Work" in result and "%" not in result


@pytest.mark.parametrize("use_kind", [True, False])
def test_api_key_is_not_a_shared_chatgpt_account(snapshot, use_kind):
    if use_kind:
        snapshot["kinds"] = {"codex:2": "api_key"}
    else:
        row = list(snapshot["accounts"][2])
        row[3] = menubar.USAGE_API_KEY
        snapshot["accounts"][2] = tuple(row)
    assert title(snapshot, "chatgpt") == "ChatGPT · No shared account"


def test_chatgpt_stale_weekly_usage_uses_its_own_fetch_clock(snapshot):
    # The Claude measurement timestamp must not roll forward this stale quota.
    snapshot["active_fetched_at"] = 200
    row = list(snapshot["accounts"][2])
    row[3] = "unavailable"
    row[4] = {"seven_day": {"pct": 95, "resets_at": "1970-01-01T00:02:30Z"}}
    row[8] = 100
    snapshot["accounts"][2] = tuple(row)
    assert "95%" in title(snapshot, "chatgpt", title_pct="7d")
    row[3] = row[4]
    snapshot["accounts"][2] = tuple(row)
    assert "0%" in title(snapshot, "chatgpt", title_pct="7d")


def test_provider_setting_persists_and_rebuilds_without_switching_accounts(tmp_path):
    tree = ast.parse(Path(menubar.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "MenuBarApp")
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {"_on_setting", "_save_and_rebuild"}]
    module = ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[]))
    scope = dict(vars(menubar))
    scope["settings_path"] = tmp_path / "menubar_settings.json"
    exec(compile(module, "<menu-provider-setting>", "exec"), scope)
    app = SimpleNamespace(
        settings=menubar.MenuBarSettings(), _panel=None,
        rebuild_menu=Mock(),
    )
    app._save_and_rebuild = lambda: scope["_save_and_rebuild"](app)
    # Exercise only the provider branch, with no switcher/auth APIs available.
    scope["_on_setting"](app, "menu_bar_provider", "both")
    assert app.settings.menu_bar_provider == "both"
    app.rebuild_menu.assert_called_once()
    assert menubar.MenuBarSettings.load(scope["settings_path"]).menu_bar_provider == "both"
    scope["_on_setting"](app, "menu_bar_provider", "bad-value")
    assert app.settings.menu_bar_provider == "both"
