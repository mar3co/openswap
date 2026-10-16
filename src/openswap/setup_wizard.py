"""The setup wizard's steps and pages (plan 021). No AppKit.

The app's first-run window walks through what ``openswap setup`` and
Settings › This Mac do: move the app into Applications when it runs from a
disk image or a download, add accounts, Open at login, and the extras. This
module decides which steps show and what each page says; the window
(``menubar_wizard``) only draws a page and reports the action a button
names, and the app (``menubar``) carries the actions out through the same
functions the terminal and Settings call.

Account buttons act at once. Everything else is a choice
(:class:`WizardChoices`) the app applies at ``finish``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

MOVE = "move"
WELCOME = "welcome"
CLAUDE = "claude"
CODEX = "codex"
LOGIN = "login"
EXTRAS = "extras"
DONE = "done"

# Actions a page's buttons and switches report to the app.
ACTION_NEXT = "next"
ACTION_BACK = "back"
ACTION_CLOSE = "close"
ACTION_MOVE = "move"
ACTION_OPEN_EXISTING = "open_existing"
ACTION_FINISH = "finish"
ACTION_COPY_COMMAND = "copy_command"
ACTION_TOGGLE_OPEN_AT_LOGIN = "toggle_open_at_login"
ACTION_TOGGLE_STATUS_LINE = "toggle_status_line"
# Account buttons, as the popover's ``(provider, empty-state action)``.
ACCOUNT_ACTIONS = {
    "claude_start": ("claude", "start"),
    "claude_add": ("claude", "add"),
    "claude_retry": ("claude", "retry"),
    "codex_start": ("chatgpt", "start"),
    "codex_capture": ("chatgpt", "capture"),
    "codex_retry": ("chatgpt", "retry"),
}

BREW_UPGRADE = "brew upgrade --cask openswap"
WIDGET_HINT = "Right-click your desktop, choose Edit Widgets, and search for OpenSwap."


@dataclass(frozen=True)
class WizardFacts:
    """What the app knows when it draws a page; read again each time.

    ``move`` is :func:`openswap.bundle.move_plan`'s answer for a copy at a
    temporary location, or ``{"action": "error", "error": ...}``. Account
    lists hold ``(title, subtitle)`` pairs as the popover's cards show them.
    ``*_state`` is the popover's roster state: ``ready``, ``loading``,
    ``error`` or ``unavailable``. ``status_line`` is ``on``, ``off`` or
    ``other`` (a hook for another OpenSwap). ``command`` is this app's
    executable, for the terminal command line; empty when not needed.
    """

    temporary: bool = False
    move: dict | None = None
    claude_accounts: tuple[tuple[str, str], ...] = ()
    claude_state: str = "ready"
    claude_signing_in: bool = False
    claude_installed: bool = True
    codex_available: bool = False
    codex_accounts: tuple[tuple[str, str], ...] = ()
    codex_state: str = "ready"
    codex_signing_in: bool = False
    login_item: bool = False
    status_line: str = "off"
    widget: bool = True
    homebrew: bool = False
    command_on_path: bool = True
    command: str = ""


@dataclass
class WizardChoices:
    """The person's picks so far; applied together at ``finish``."""

    open_at_login: bool = True
    status_line: bool = False


def initial_choices(facts: WizardFacts) -> WizardChoices:
    """Open at login starts on; the status line starts as it is on disk."""
    return WizardChoices(open_at_login=True, status_line=facts.status_line == "on")


def wizard_steps(facts: WizardFacts) -> tuple[str, ...]:
    """The steps for these facts, in order.

    A copy at a temporary location shows only Move: nothing else may be
    pinned to a path that disappears, so setup continues in the moved copy.
    """
    if facts.temporary:
        return (MOVE,)
    steps = [WELCOME, CLAUDE]
    if facts.codex_available:
        steps.append(CODEX)
    steps += [LOGIN, EXTRAS, DONE]
    return tuple(steps)


def next_step(step: str, facts: WizardFacts) -> str:
    steps = wizard_steps(facts)
    if step not in steps:
        return steps[0]
    return steps[min(steps.index(step) + 1, len(steps) - 1)]


def previous_step(step: str, facts: WizardFacts) -> str:
    steps = wizard_steps(facts)
    if step not in steps:
        return steps[0]
    return steps[max(steps.index(step) - 1, 0)]


def _progress(step: str, facts: WizardFacts) -> tuple[int, int] | None:
    """(1-based position, count) among the steps that show dots."""
    shown = [s for s in wizard_steps(facts) if s not in (MOVE,)]
    if step not in shown:
        return None
    return shown.index(step) + 1, len(shown)


def _note(text: str, tone: str = "muted") -> dict:
    return {"kind": "note", "text": text, "tone": tone}


def _move_page(facts: WizardFacts) -> dict:
    plan = facts.move or {}
    source = plan.get("source")
    if source == "disk_image":
        body = "OpenSwap is running from the disk image. Move it to Applications to finish setting up."
    else:
        body = "OpenSwap is running from your download. Move it to Applications to finish setting up."
    rows: list[dict] = []
    destination = plan.get("destination") or "Applications"
    action = plan.get("action")
    primary = {"title": "Move to Applications", "action": ACTION_MOVE, "enabled": True}
    if action == "error":
        rows.append(_note(f"Couldn’t move OpenSwap: {plan.get('error') or 'unknown error'}", "warning"))
        rows.append(_note("Drag it into your Applications folder in Finder, then open it from there."))
        primary = None
    elif action == "open_existing" and plan.get("homebrew"):
        rows.append(_note(f"Homebrew already installed OpenSwap in {destination}."))
        rows.append(_note(f"To update it, run: {BREW_UPGRADE}"))
        primary = {"title": "Open Installed Copy", "action": ACTION_OPEN_EXISTING, "enabled": True}
    elif action == "open_existing":
        version = plan.get("existing_version")
        what = f"OpenSwap {version}" if version else "OpenSwap"
        rows.append(_note(f"{what} is already in {destination}, and it’s up to date."))
        primary = {"title": "Open Installed Copy", "action": ACTION_OPEN_EXISTING, "enabled": True}
    elif action == "replace":
        version = plan.get("existing_version")
        what = f"An older version ({version})" if version else "An older version"
        rows.append(_note(f"{what} is already in {destination}. It will be moved to the Trash."))
        primary = {"title": "Replace and Open", "action": ACTION_MOVE, "enabled": True}
    else:
        rows.append(_note(f"OpenSwap will be copied to {destination} and opened from there."))
    if source == "disk_image" and primary is not None:
        rows.append(_note("The disk image will be ejected when it’s done."))
    return {
        "title": "Move OpenSwap to Applications",
        "body": body,
        "rows": rows,
        "primary": primary,
        "secondary": {"title": "Not Now", "action": ACTION_CLOSE},
        "back": False,
    }


def _welcome_page() -> dict:
    return {
        "title": "Welcome to OpenSwap",
        "body": "OpenSwap manages your Claude Code and Codex accounts from the menu bar.",
        "icon": True,
        "rows": [
            {
                "kind": "features",
                "items": [
                    ("gauge.with.needle", "See your usage",
                     "How much of each account you’ve used, in the menu bar and a widget."),
                    ("arrow.left.arrow.right", "Switch in one click",
                     "Switch Claude Code or Codex to another account without signing out."),
                    ("sparkles", "Or let OpenSwap switch for you",
                     "It can move to the next account before one hits its limit."),
                ],
            },
            _note("This takes about a minute. Nothing changes until you say so."),
        ],
        "primary": {"title": "Get Started", "action": ACTION_NEXT, "enabled": True},
        "secondary": None,
        "back": False,
    }


def _account_page(provider: str, facts: WizardFacts) -> dict:
    claude = provider == CLAUDE
    accounts = facts.claude_accounts if claude else facts.codex_accounts
    state = facts.claude_state if claude else facts.codex_state
    signing_in = facts.claude_signing_in if claude else facts.codex_signing_in
    name = "Claude" if claude else "Codex"
    rows: list[dict] = []
    if accounts:
        title = f"Your {name} accounts"
        body = "These are saved. Add another now, or later from the menu bar."
        rows.append({"kind": "accounts", "items": [tuple(a) for a in accounts]})
    elif claude:
        title = "Add your Claude account"
        body = (
            "Sign in with your browser, or save the account Claude Code is "
            "already signed in to. You can add more later from the menu bar."
        )
    else:
        title = "Add a Codex account"
        body = (
            "Optional. If you use Codex, OpenSwap can switch its accounts too. "
            "Sign in with your browser, or save the account Codex is already "
            "signed in to."
        )

    enabled = True
    if state == "loading":
        rows.append(_note(f"Loading your saved {name} accounts…"))
        enabled = False
    elif state == "error":
        rows.append(_note(f"Couldn’t load your {name} accounts. Nothing was changed.", "warning"))
        rows.append({"kind": "actions", "items": [
            {"title": "Try Again", "action": f"{provider}_retry", "enabled": True},
        ]})
        enabled = False
    elif state == "unavailable":
        rows.append(_note(
            "Codex’s account list isn’t available right now. Restart OpenSwap and try again.",
            "warning",
        ))
        enabled = False
    if claude and not facts.claude_installed:
        rows.append(_note(
            "Claude Code isn’t installed. Install it first, then add your "
            "account here or from the menu bar.",
            "warning",
        ))
        enabled = False
    if enabled:
        sign_in = "Sign in with Claude" if claude else "Sign in with ChatGPT"
        if signing_in:
            rows.append(_note(
                "Finish signing in in your browser. Your account will show up here when it’s done."
            ))
            sign_in = "Open Sign-in Again"
        rows.append({"kind": "actions", "items": [
            # The page's one filled, accent button until an account is saved;
            # Continue takes that role afterwards.
            {"title": sign_in, "action": f"{provider}_start", "enabled": True,
             "primary": not accounts},
            {
                "title": "Save Current Login",
                "action": f"{provider}_add" if claude else f"{provider}_capture",
                "enabled": not signing_in,
            },
        ]})
    return {
        "title": title,
        "body": body,
        "rows": rows,
        "primary": {"title": "Continue", "action": ACTION_NEXT, "enabled": True} if accounts else None,
        "secondary": None,
        "skip": None if accounts else {"title": "Skip for Now", "action": ACTION_NEXT},
        "back": True,
    }


def _login_page(facts: WizardFacts, choices: WizardChoices) -> dict:
    hint = (
        "OpenSwap already opens at login."
        if facts.login_item
        else "Starts OpenSwap when you log in to your Mac."
    )
    return {
        "title": "Open at login",
        "body": "OpenSwap lives in the menu bar, so it’s most useful when it’s always running.",
        "rows": [
            {
                "kind": "switch",
                "symbol": "power",
                "action": ACTION_TOGGLE_OPEN_AT_LOGIN,
                "label": "Open OpenSwap at login",
                "value": bool(choices.open_at_login),
                "disabled": False,
                "hint": hint,
            },
        ],
        "primary": {"title": "Continue", "action": ACTION_NEXT, "enabled": True},
        "secondary": None,
        "back": True,
    }


def terminal_command(executable: str) -> str:
    """The line that puts this app's ``openswap`` command on PATH."""
    return f"ln -s {shlex.quote(executable)} ~/.local/bin/openswap"


def _extras_page(facts: WizardFacts, choices: WizardChoices) -> dict:
    if facts.status_line == "other" and not choices.status_line:
        status_hint = (
            "Claude Code’s status line is using another copy of OpenSwap. "
            "Turn this on to use this one."
        )
    else:
        status_hint = "Adds the account name next to your usage. Your own status line keeps working."
    rows: list[dict] = [
        {
            "kind": "switch",
            "symbol": "text.alignleft",
            "action": ACTION_TOGGLE_STATUS_LINE,
            "label": "Show your account in Claude Code’s status line",
            "value": bool(choices.status_line),
            "disabled": False,
            "hint": status_hint,
        },
    ]
    if facts.widget:
        rows.append({"kind": "item", "symbol": "square.grid.2x2", "title": "Widget", "text": WIDGET_HINT})
    if not facts.homebrew and not facts.command_on_path and facts.command:
        rows.append({
            "kind": "command",
            "symbol": "terminal",
            "title": "Terminal command",
            "text": "To use the openswap command in Terminal, run:",
            "command": terminal_command(facts.command),
            "action": ACTION_COPY_COMMAND,
        })
    return {
        "title": "A few extras",
        "body": "All optional. You can change them later in Settings › General.",
        "rows": rows,
        "primary": {"title": "Continue", "action": ACTION_NEXT, "enabled": True},
        "secondary": None,
        "back": True,
    }


def _done_page(facts: WizardFacts, choices: WizardChoices) -> dict:
    claude = len(facts.claude_accounts)
    summary = [
        ("Claude accounts", str(claude) if claude else "None yet"),
    ]
    if facts.codex_available:
        codex = len(facts.codex_accounts)
        summary.append(("Codex accounts", str(codex) if codex else "None yet"))
    summary.append(("Open at login", "On" if choices.open_at_login else "Off"))
    summary.append(("Claude Code status line", "On" if choices.status_line else "Off"))
    return {
        "title": "You’re all set",
        "body": "OpenSwap is in your menu bar. Click its icon to see your accounts and switch between them.",
        "rows": [{"kind": "summary", "items": summary}],
        "primary": {"title": "Open OpenSwap", "action": ACTION_FINISH, "enabled": True},
        "secondary": None,
        "back": False,
    }


def wizard_page(step: str, facts: WizardFacts, choices: WizardChoices) -> dict:
    """Everything the window draws for ``step``.

    ``title``, ``body``, ``rows`` (each a dict with a ``kind``), ``primary``
    (``{"title", "action", "enabled"}`` or None: the one filled, accent
    button; an ``actions`` row item marked ``primary`` takes that role
    instead), ``secondary`` (a filled gray button, or None), ``skip`` (a
    plain text button, or None), ``back`` (a plain text button), ``icon``
    (the app icon above the title) and ``progress`` (``(n, total)`` or None).
    """
    if step not in wizard_steps(facts):
        step = wizard_steps(facts)[0]
    if step == MOVE:
        page = _move_page(facts)
    elif step == WELCOME:
        page = _welcome_page()
    elif step in (CLAUDE, CODEX):
        page = _account_page(step, facts)
    elif step == LOGIN:
        page = _login_page(facts, choices)
    elif step == EXTRAS:
        page = _extras_page(facts, choices)
    else:
        page = _done_page(facts, choices)
    page["step"] = step
    page["progress"] = _progress(step, facts)
    page.setdefault("skip", None)
    page.setdefault("icon", False)
    return page
