"""Command-line interface for OpenSwap."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

from openswap import __version__, paths, printer
from openswap.exceptions import ClaudeSwitchError
from openswap.json_output import error_envelope
from openswap.printer import (
    accent,
    bolded,
    dimmed,
    error,
    force_utf8_output,
    muted,
    warning,
)
from openswap.settings import load_ui_settings
from openswap.engine import Engine

# Same object as Engine. Tests patch ``openswap.cli.ClaudeAccountSwitcher``;
# constructions still instantiate the public façade.
ClaudeAccountSwitcher = Engine


def _frozen_without_terminal() -> bool:
    """True when the frozen .app was started by Finder / LaunchServices."""
    if not getattr(sys, "frozen", False):
        return False
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return True  # no usable stdin at all: not a terminal


def _prog_name() -> str:
    """The command name to show in usage/help.

    argparse otherwise defaults to ``os.path.basename(sys.argv[0])``, which for
    an installed entry-point shim renders as an ugly absolute path (e.g.
    ``python.exe C:\\Users\\me\\.local\\bin\\openswap``). We strip that down to the
    bare command the user typed (``openswap`` / ``openswap``), falling back to
    ``openswap`` for ``python -m openswap`` and odd launchers.
    """
    name = os.path.basename(sys.argv[0] or "")
    for ext in (".exe", ".pyw", ".py"):
        if name.lower().endswith(ext):
            name = name[: -len(ext)]
            break
    if not name or name in {"__main__", "python", "python3", "py"}:
        return "openswap"
    return name


# Memorable subcommand aliases → the long-standing flags they expand to. Lets
# users type `openswap list`, `openswap status`, `openswap add`, etc. instead of `--list`
# / `--status` / `--add-account`, which all still work. `switch` is special-cased
# below (a bare `switch` rotates; `switch <target>` jumps to one account) and
# `auto` keeps its own pre-dispatch parser, so none of those are listed here.
_SUBCOMMAND_FLAGS = {
    "help": "--help",
    "list": "--list",
    "ls": "--list",
    "status": "--status",
    "add": "--add-account",
    "add-token": "--add-token",
    "remove": "--remove-account",
    "rm": "--remove-account",
    "disable": "--disable-account",
    "enable": "--enable-account",
    "export": "--export",
    "import": "--import",
    "purge": "--purge",
    "upgrade": "--upgrade",
    "update": "--upgrade",
    "menubar": "--menubar",
}


def _translate_subcommand(argv: list[str]) -> list[str]:
    """Rewrite a leading memorable subcommand into the equivalent flag argv.

    ``argv`` is the args after the program name. The rewrite only fires when the
    first token is a recognized verb (which never starts with '-'), so the
    established ``--flag`` interface — and every existing test that drives it —
    is left untouched. Tokens after the verb pass through verbatim, so flags
    like ``--json``, ``--strategy``, ``--slot``, and ``--force`` keep combining
    exactly as before (e.g. ``openswap switch --strategy best``, ``openswap list --json``).
    """
    if not argv:
        return argv

    verb, rest = argv[0], argv[1:]

    if verb == "switch":
        # Bare `switch` rotates; `switch <num|email>` jumps to that account.
        if rest and not rest[0].startswith("-"):
            return ["--switch-to", *rest]
        return ["--switch", *rest]

    flag = _SUBCOMMAND_FLAGS.get(verb)
    if flag is not None:
        return [flag, *rest]

    return argv


def _guard_root(switcher: Engine) -> None:  # Engine ≡ ClaudeAccountSwitcher
    """Refuse to run as root outside a container."""
    if sys.platform != "win32":
        if os.geteuid() == 0 and not switcher._is_running_in_container():
            error("Error: Do not run this script as root (unless running in a container)")
            sys.exit(1)


def _unclaimed_command(argv: list[str]) -> None:
    """Handle `openswap unclaimed [--purge ID]` — inspect or drop a stash row.

    The stash holds credential bytes a switch or a consume gate could not
    attribute to a slot. Rows normally clear themselves (the next gate pass
    adopts or retires them), but two states need a human: a row whose bytes
    are unreadable until a keychain is unlocked or a mode is fixed, and one
    whose metadata was lost, which no pass can ever adopt. ``--json`` lists
    only bare ids, so without this there is nothing to look at and nothing to
    drop short of hand-editing the manifest.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} unclaimed",
        description=(
            "List stashed credential entries, or purge one by id. "
            "Purging deletes the bytes — recovery is /login + `openswap add`."
        ),
    )
    parser.add_argument(
        "--purge",
        metavar="ID",
        help="Delete this entry's bytes and manifest row",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        entries = switcher.list_unclaimed_credentials()

        if args.purge:
            if args.purge not in entries:
                error(f"Error: no unclaimed entry {args.purge}")
                sys.exit(1)
            switcher._store._remove_unclaimed_credential(args.purge)
            print(f"{accent('Purged')} {args.purge}")
            return

        if not entries:
            print(dimmed("No unclaimed credential entries"))
            return
        for entry_id, meta in sorted(entries.items()):
            slot = meta.get("configSlot") or "?"
            reason = meta.get("reason") or "orphaned (no manifest row)"
            print(f"{entry_id}  slot {slot}  {reason}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _swap_command(argv: list[str]) -> None:
    """Handle `openswap swap NUM|EMAIL|ALIAS NUM|EMAIL|ALIAS`.

    Exchanges the two accounts' slot numbers (list order and numeric
    targets). Pre-dispatched before the main parser for the same reason as
    `alias` (the main parser's required mutually-exclusive group can't hold
    a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} swap",
        description=(
            "Exchange two accounts' slot numbers, so they trade places in "
            "`openswap list` and as numeric targets. Aliases, backups, and "
            "session history move with their account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  openswap swap 1 2
  openswap swap dev user@example.com
        """,
    )
    parser.add_argument("first", metavar="NUM|EMAIL|ALIAS", help="One account")
    parser.add_argument("second", metavar="NUM|EMAIL|ALIAS", help="The other account")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_a, num_b = switcher.swap_accounts(args.first, args.second)
        print(f"{accent('Swapped')} Account {num_a} and Account {num_b}:")
        data = switcher.sequence_data() or {}
        accounts = data.get("accounts", {})
        for num in sorted((num_a, num_b), key=int):
            email = accounts.get(num, {}).get("email", "")
            print(f"  {num}: {email}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _move_command(argv: list[str]) -> None:
    """Handle `openswap move NUM|EMAIL|ALIAS SLOT`.

    Assigns an account to a specific slot number. If the slot is empty the
    account is relocated there (its old slot is freed); if it is occupied the
    two accounts trade places. `swap a b` is exactly `move a <b's slot>`.
    Pre-dispatched before the main parser for the same reason as `alias`.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} move",
        description=(
            "Assign an account to a slot number. An empty slot relocates the "
            "account there and frees its old slot; an occupied slot swaps the "
            "two. Aliases, backups, and session history move with the account."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  openswap move user@example.com 1   move an account onto shortcut 1
  openswap move dev 1                by alias
  openswap move 2 1                  by number (swaps if slot 1 is taken)
        """,
    )
    parser.add_argument("account", metavar="NUM|EMAIL|ALIAS", help="Account to move")
    parser.add_argument("slot", metavar="SLOT", help="Destination slot number")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)
        num_src, num_target, swapped = switcher.move_account(args.account, args.slot)
        data = switcher.sequence_data() or {}
        accounts = data.get("accounts", {})
        if num_src == num_target:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{dimmed('Already in')} slot {num_target}: {email}")
        elif swapped:
            print(f"{accent('Swapped')} Account {num_src} and Account {num_target}:")
            for num in sorted((num_src, num_target), key=int):
                email = accounts.get(num, {}).get("email", "")
                print(f"  {num}: {email}")
        else:
            email = accounts.get(num_target, {}).get("email", "")
            print(f"{accent('Moved')} {email} to slot {num_target}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _alias_command(argv: list[str]) -> None:
    """Handle `openswap alias [NUM|EMAIL] [NAME] [--unset]`.

    With no arguments, lists all aliases. Otherwise sets (or, with --unset,
    removes) the alias for the given account. Pre-dispatched before the main
    parser for the same reason as `auto` (the main parser's required
    mutually-exclusive group can't hold a positional subcommand).
    """
    parser = argparse.ArgumentParser(
        prog="openswap alias",
        description=(
            "Set, remove, or list a short display alias for an account. "
            "Once set, the alias can be used anywhere an account number or "
            "email is accepted (switch, remove)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  openswap alias 2 dev
  openswap alias user@example.com dev
  openswap alias 2 --unset
  openswap alias                         # list all aliases
        """,
    )
    parser.add_argument(
        "account",
        nargs="?",
        metavar="NUM|EMAIL",
        help="Account to alias (number or email). Omit to list aliases.",
    )
    parser.add_argument(
        "alias_name",
        nargs="?",
        metavar="NAME",
        help="Alias to set (letters, digits, ., -, _; not purely numeric).",
    )
    parser.add_argument("--unset", action="store_true", help="Remove the account's alias")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    if args.unset and args.alias_name:
        parser.error("--unset does not take a NAME argument")
    if args.unset and args.account is None:
        parser.error("NUM|EMAIL is required with --unset")
    if args.account is not None and not args.unset and not args.alias_name:
        parser.error("NAME is required (or pass --unset to remove the alias)")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        _guard_root(switcher)

        if args.account is None:
            rows = switcher.list_aliases()
            if not rows:
                print(dimmed("No aliases set"))
                return
            print(bolded("Aliases:"))
            for num, alias_name, email in rows:
                print(f"  {num}: {alias_name} {muted(f'({email})')}")
            return

        if args.unset:
            account_num = switcher.unset_alias(args.account)
            print(f"{accent('Removed alias')} for Account {account_num}")
        else:
            account_num, normalized = switcher.set_alias(args.account, args.alias_name)
            print(f"{accent('Set alias')} '{normalized}' for Account {account_num}")
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        sys.exit(130)


def _codex_command(argv: list[str]) -> int:
    """Handle ``openswap codex add|list|switch|remove|disable|enable|alias``."""
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} codex",
        description="Manage Codex CLI accounts as a second provider beside Claude.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="verb", required=True)

    add_p = sub.add_parser("add", help="Capture the current Codex login")
    add_p.add_argument("--alias", metavar="NAME", help="Short alias for the new slot")

    list_p = sub.add_parser("list", help="List managed Codex accounts")
    list_p.add_argument("--json", action="store_true", help="Emit JSON")

    sw = sub.add_parser("switch", help="Switch the live Codex login")
    sw.add_argument("target", nargs="?", metavar="NUM|EMAIL|ALIAS")
    sw.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        help="Pick a target by remaining quota instead of a slot",
    )
    sw.add_argument("--force", action="store_true", help="Overwrite an unmanaged live login")
    sw.add_argument("--json", action="store_true", help="Emit JSON")

    rm = sub.add_parser("remove", help="Remove a Codex account")
    rm.add_argument("target", metavar="NUM|EMAIL|ALIAS")
    rm.add_argument("-y", "--yes", action="store_true", dest="assume_yes")

    dis = sub.add_parser("disable", help="Hold a Codex account out of rotation")
    dis.add_argument("target", metavar="NUM|EMAIL|ALIAS")
    en = sub.add_parser("enable", help="Return a Codex account to rotation")
    en.add_argument("target", metavar="NUM|EMAIL|ALIAS")

    al = sub.add_parser("alias", help="Set or unset a Codex account alias")
    al.add_argument("target", metavar="NUM|EMAIL|ALIAS")
    al.add_argument("name", nargs="?", metavar="NAME")
    al.add_argument("--unset", action="store_true", help="Remove the alias")

    args = parser.parse_args(argv)
    from openswap.codex.engine import CodexEngine

    try:
        eng = CodexEngine(debug=args.debug)
        if args.verb == "add":
            num = eng.add_account(alias=args.alias)
            print(f"Added Codex account {num} ({eng.account_email(num)})")
            return 0
        if args.verb == "list":
            payload = eng.list_accounts(json_output=args.json)
            if args.json:
                print(json.dumps(payload, indent=2))
            return 0
        if args.verb == "switch":
            if args.target:
                result = eng.switch_to(
                    args.target, json_output=args.json, force=args.force
                )
            else:
                result = eng.switch(
                    strategy=args.strategy, json_output=args.json, force=args.force
                )
            if args.json:
                print(json.dumps(result, indent=2))
            elif result:
                dest = (result.get("to") or {}).get("email") or ""
                if result.get("switched"):
                    print(f"Switched Codex login to {dest}")
                else:
                    print(f"Codex login already {dest or 'active'}")
            return 0 if result and result.get("switched") else 2
        if args.verb == "remove":
            eng.remove_account(args.target, assume_yes=args.assume_yes)
            return 0
        if args.verb == "disable":
            eng.set_account_disabled(args.target, True)
            print(f"Disabled Codex account {args.target}")
            return 0
        if args.verb == "enable":
            eng.set_account_disabled(args.target, False)
            print(f"Enabled Codex account {args.target}")
            return 0
        if args.verb == "alias":
            if args.unset:
                num = eng.unset_alias(args.target)
                print(f"Removed alias for Codex account {num}")
            else:
                if not args.name:
                    parser.error("NAME is required (or pass --unset)")
                num, normalized = eng.set_alias(args.target, args.name)
                print(f"Set alias '{normalized}' for Codex account {num}")
            return 0
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        return 1
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        return 130
    return 1


def _auto_command(argv: list[str]) -> None:
    """Handle `openswap auto [--once] [--json] [...]`.

    Pre-dispatched before the main parser is built (and with the
    same limitation: `auto` must be the first argument). Runs the auto-switch
    engine — a foreground loop by default, or a single evaluate-and-maybe-
    switch tick with --once whose exit code reports the outcome (for cron/
    systemd timers): 0 switched, 1 error, 2 no action needed, 3 blocked
    (no viable target / all accounts exhausted).
    """
    import signal
    import time as _time

    parser = argparse.ArgumentParser(
        prog="openswap auto",
        description=(
            "Automatically switch accounts when the active one nears its "
            "5h/7d rate limit. Runs a foreground polling loop; use --once "
            "for a single tick (cron-friendly)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes with --once:
  0  switched to another account
  1  error (network trouble, lock contention, ...)
  2  no action needed
  3  blocked: wanted to switch but no viable target / all exhausted

Examples:
  openswap auto                       # foreground loop, switch at 90%% used
  openswap auto --threshold 80        # switch earlier
  openswap auto --model Fable         # also switch when the Fable weekly limit is hit
  openswap auto --json                # one JSON event per line (for scripts)
  openswap auto --once; echo $?       # single tick, outcome in exit code
  openswap auto --dry-run             # log decisions, never actually switch

Defaults live in settings.json (shared policy); flags override them.
Codex rotation runs alongside; its outcome is logged, not returned.
        """,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate once, maybe switch, and exit (exit code = outcome)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one machine-readable JSON event per line on stdout",
    )
    parser.add_argument(
        "--interval",
        type=float,
        metavar="SECONDS",
        help="Poll interval in loop mode (min 15; default 60)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        metavar="PCT",
        help=(
            "Switch when the active account's binding 5h/7d window reaches "
            "this utilization (50-99.9; default 90)"
        ),
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        metavar="SECONDS",
        help="Minimum time between proactive switches (default 300)",
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "Also switch when a per-model weekly limit is hit, not just the "
            "account-wide 5h/7d windows. One name or a comma-separated list "
            "(e.g. Fable, Opus, Sonnet, Haiku, or 'Fable,Opus'), or 'all' "
            "for every per-model window an account reports"
        ),
    )
    parser.add_argument(
        "--include-api-key-accounts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Allow switching onto managed API-key accounts as a last resort "
            "(they bill per token; default: excluded)"
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=("best", "consume-first", "soonest-5h"),
        default=None,
        help=(
            "Target selection: 'best' (most quota left; default), "
            "'consume-first' (burn weekly first: the account whose 7-day "
            "window resets soonest), or 'soonest-5h' (burn 5-hour first: "
            "the account whose 5-hour session resets soonest)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report, but never switch or write state",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    from openswap.autoswitch import AutoSwitchEngine, AutoSwitchEvent
    from openswap.printer import accent, yellowed
    from openswap.settings import load_settings, merged_with_cli

    def jsonl_emit(event: AutoSwitchEvent) -> None:
        print(json.dumps(event.to_json()), flush=True)

    def human_emit(event: AutoSwitchEvent) -> None:
        stamp = _time.strftime("%H:%M:%S")
        line = event.human()
        if event.kind == "switch":
            line = accent(line)
        elif event.kind in ("error", "account-quarantined"):
            line = yellowed(line)
        elif event.kind in ("poll", "no-switch", "sleep"):
            line = dimmed(line)
        print(f"{stamp}  {line}", flush=True)

    def _prefixed(emit, provider: str):
        def wrapped(event: AutoSwitchEvent) -> None:
            emit(replace(event, provider=provider))
        return wrapped

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        settings = merged_with_cli(load_settings(switcher.backup_dir), args)
        emit = jsonl_emit if args.json else human_emit
        engine = AutoSwitchEngine(
            switcher,
            settings,
            emit,
            dry_run=args.dry_run,
        )

        from openswap.codex.engine import CodexEngine

        codex = CodexEngine(debug=args.debug)
        codex_engine = None
        if codex.switchable_account_numbers():
            codex_engine = AutoSwitchEngine(
                codex, settings, _prefixed(emit, "codex"), dry_run=args.dry_run,
                state_path=codex.state_dir / "autoswitch_state.json",
            )

        if args.once:
            outcome = engine.tick()
            if codex_engine is not None:
                codex_engine.tick()
            sys.exit(outcome.value)

        # Loop mode: SIGTERM (systemd stop) exits the loop cleanly.
        def _stop_all(*_):
            engine.stop()
            if codex_engine is not None:
                codex_engine.stop()

        signal.signal(signal.SIGTERM, _stop_all)
        if not args.json:
            print(
                dimmed(
                    f"Auto-switch running: threshold {settings.threshold:.0f}%, "
                    f"every {settings.interval_seconds:.0f}s"
                    f"{' (dry-run)' if args.dry_run else ''} — Ctrl-C to stop"
                )
            )
        if codex_engine is not None:
            import threading
            threading.Thread(target=codex_engine.run_loop, daemon=True).start()
        try:
            sys.exit(engine.run_loop())
        finally:
            _stop_all()
    except ClaudeSwitchError as e:
        if args.json:
            print(json.dumps(error_envelope(e)))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Auto-switch stopped')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)


def _config_command(argv: list[str]) -> None:
    """Handle `openswap config [list|get KEY|set KEY VALUE|unset KEY|path]`.

    Pre-dispatched before the main parser is built, like `auto`
    (same limitation: `config` must be the first argument). Edits shared
    policy in settings.json with strict validation, unlike loading, which
    forgivingly clamps. A typo'd key or out-of-range value errors loudly
    here instead of silently degrading at `openswap auto` time. Extra display
    and kickoff are menubar_settings.json (popover Settings), not this command.
    """
    from openswap.settings import (
        SETTING_SPECS,
        effective_settings,
        format_setting_value,
        set_setting,
        setting_spec,
        settings_path,
        unset_setting,
    )

    key_lines = "\n".join(
        f"  {spec.dotted:<34}{spec.help} (default {format_setting_value(spec.default)})"
        for spec in SETTING_SPECS.values()
    )
    parser = argparse.ArgumentParser(
        prog="openswap config",
        description=(
            "Read and edit shared policy (settings.json). Extra display "
            "and kickoff live in menubar_settings.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Keys:
{key_lines}

Examples:
  openswap config                              # list effective settings
  openswap config get autoswitch.threshold
  openswap config set autoswitch.threshold 80
  openswap config unset autoswitch.threshold   # back to the default
  openswap config path                         # where settings.json (policy) lives
        """,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout (with list or get)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    sub = parser.add_subparsers(dest="action", metavar="{list,get,set,unset,path}")

    p_list = sub.add_parser("list", help="Show all effective settings (the default)")
    p_get = sub.add_parser("get", help="Print one setting's effective value")
    p_get.add_argument("key", metavar="KEY", help="Dotted key, e.g. autoswitch.threshold")
    for p in (p_list, p_get):
        # SUPPRESS: without it the subparser's False default would clobber a
        # pre-verb `openswap config --json` in the shared namespace.
        p.add_argument(
            "--json",
            action="store_true",
            default=argparse.SUPPRESS,
            help="Emit machine-readable JSON to stdout",
        )
    p_set = sub.add_parser("set", help="Validate and persist one setting")
    p_set.add_argument("key", metavar="KEY")
    p_set.add_argument("value", metavar="VALUE")
    p_unset = sub.add_parser("unset", help="Remove one setting (revert to the default)")
    p_unset.add_argument("key", metavar="KEY")
    sub.add_parser("path", help="Print the settings.json (policy) location")

    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    action = args.action or "list"
    if json_mode and action not in ("list", "get"):
        parser.error("--json can only be used with list or get")

    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)
        root = switcher.backup_dir

        if action == "path":
            print(settings_path(root))
        elif action == "list":
            rows = effective_settings(root)
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "path": str(settings_path(root)),
                    "settings": [
                        {"key": spec.dotted, "value": value, "isSet": is_set}
                        for spec, value, is_set in rows
                    ],
                }
                print(json.dumps(payload, indent=2))
            else:
                key_w = max(len(spec.dotted) for spec, _, _ in rows)
                val_w = max(len(format_setting_value(v)) for _, v, _ in rows)
                for spec, value, is_set in rows:
                    line = f"{spec.dotted:<{key_w}}  {format_setting_value(value):<{val_w}}"
                    print(line if is_set else f"{line}  {dimmed('(default)')}")
        elif action == "get":
            spec = setting_spec(args.key)
            value, is_set = next(
                (v, s) for sp, v, s in effective_settings(root) if sp is spec
            )
            if json_mode:
                payload = {
                    "schemaVersion": 1,
                    "key": spec.dotted,
                    "value": value,
                    "isSet": is_set,
                }
                print(json.dumps(payload, indent=2))
            else:
                print(format_setting_value(value))
        elif action == "set":
            value = set_setting(root, args.key, args.value)
            print(f"{args.key} = {format_setting_value(value)}")
        elif action == "unset":
            if unset_setting(root, args.key):
                default = setting_spec(args.key).default
                print(f"{args.key} unset (default: {format_setting_value(default)})")
            else:
                print(muted(f"{args.key} is not set; nothing to do"), file=sys.stderr)
    except ClaudeSwitchError as e:
        if json_mode:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if json_mode else sys.stdout,
        )
        sys.exit(130)


def _use_native_tls() -> None:
    """Route TLS trust decisions through the OS-native verifier.

    Claude's token endpoint (``platform.claude.com``) serves a Let's Encrypt
    chain. Python's stdlib ``ssl`` uses OpenSSL, which on Windows loads the
    system cert store as a flat set and matches CA certs by *subject name*, so a
    stale, expired duplicate of an intermediate (e.g. an old ``ISRG Root X2``
    left in the user's store) can shadow the valid path and fail verification
    with "certificate has expired" even though the served chain is valid — which
    silently breaks inactive-account token refresh. The OS-native verifiers
    (SChannel on Windows, SecureTransport on macOS) build the chain correctly
    and don't trip on the expired duplicate — the same reason Claude Code (Node,
    with its own bundled roots) is unaffected. ``truststore`` delegates to them.

    Best-effort: on any failure fall back to stdlib ``ssl`` rather than block
    the CLI over a TLS-trust nicety.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def _widget_command(argv: list[str]) -> int:
    """Handle ``openswap widget --install|--uninstall|--status``.

    Pre-dispatched like ``auto`` so it does not require a switcher
    (building the widget must work on a fresh machine before any account is
    added). macOS-only; the install module raises ClaudeSwitchError elsewhere.
    """
    parser = argparse.ArgumentParser(
        prog="openswap widget",
        description=(
            "Install the macOS Desktop and Notification Center widget for "
            "openswap usage. The menu bar extra writes a snapshot; this signed "
            "WidgetKit app is what macOS actually shows."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
After install, add the widget:
  Notification Center: click the date in the menu bar, Edit Widgets
  Desktop: right-click the desktop, Edit Widgets
  Look for "OpenSwap"

The menu bar extra must be running so the widget has live usage numbers.
""",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--install",
        action="store_true",
        help="Build the widget app, copy it to ~/Applications, and start it",
    )
    group.add_argument(
        "--uninstall",
        action="store_true",
        help="Stop the widget host and remove ~/Applications/OpenSwap.app",
    )
    group.add_argument(
        "--status",
        action="store_true",
        help="Report whether the widget app and its LaunchAgent are present",
    )
    args = parser.parse_args(argv)
    from openswap.widget_install import (
        install_widget,
        uninstall_widget,
        widget_status,
    )

    try:
        if args.install:
            result = install_widget()
            print(f"Widget installed ({result['app']}).")
            print(
                dimmed(
                    "Add it from Notification Center, or right-click the "
                    "desktop and choose Edit Widgets. Look for OpenSwap."
                )
            )
            print(dimmed("The menu bar extra must be running for live numbers."))
            return 0
        if args.uninstall:
            result = uninstall_widget()
            if result.get("removed_app") or result.get("removed_plist") or result.get("was_loaded"):
                print("Widget removed.")
            else:
                print("Widget was not installed.")
            return 0
        state = widget_status()
        app_state = "present" if state["app_installed"] else "missing"
        print(f"Widget app: {app_state} ({state['app']})")
        if not state["installed"] and not state["loaded"]:
            print("Widget host service is not installed.")
            print(dimmed("Install it with: openswap widget --install"))
            return 0
        host = state["state"] or ("loaded" if state["loaded"] else "stopped")
        pid = f" (pid {state['pid']})" if state["pid"] else ""
        print(f"Widget host: {host}{pid}")
        return 0
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        return 1


def _wait_for_pid(launch_agent, label: str, timeout: float = 3.0) -> int | None:
    """The extra's pid once launchd has spawned it, or None if it has not by
    ``timeout``. bootstrap returns before the process exists, so a plain
    status read right after it would report a healthy job as absent."""
    import time

    deadline = time.monotonic() + timeout
    while True:
        pid = launch_agent.status(label)["pid"]
        if pid or time.monotonic() >= deadline:
            return pid
        time.sleep(0.2)


def _setup_command(argv: list[str]) -> int:
    """Handle ``openswap setup``: save the live Claude login, start the extra.

    The install script ends here so a fresh Mac goes from one command to an
    icon in the menu bar. Re-runnable: add_account refreshes an account that
    is already stored, and launch_agent.install re-bootstraps the service.
    No Claude login is a warning and exit 0: the extra still installs and can
    capture the account later. Any other capture failure (Keychain denied,
    lock timeout, ...) still installs the extra but exits 1, because "log in"
    is the wrong advice for it. A failed capture is reported after the extra
    so its hint is the last thing the user reads.
    """
    parser = argparse.ArgumentParser(
        prog="openswap setup",
        description=(
            "Save the Claude account you are logged into and start the "
            "menu bar extra (now, and at every login)."
        ),
    )
    parser.parse_args(argv)
    from openswap import launch_agent
    from openswap.exceptions import NotLoggedInError
    from openswap.update_check import restart_widget_agent

    saved = False
    not_saved: tuple[str, str, int] | None = None  # (reason, hint, exit code)
    try:
        switcher = ClaudeAccountSwitcher()
        _guard_root(switcher)
        try:
            switcher.add_account()
            saved = True
        except NotLoggedInError as e:
            not_saved = (str(e), "Log into Claude Code, then run: openswap add", 0)
        except (ClaudeSwitchError, OSError) as e:
            # OSError: the backup store itself is unwritable. The extra is
            # still worth installing; the store problem is the user's fix.
            not_saved = (str(e), "When that is fixed, run: openswap add", 1)
        result = launch_agent.install()
        widget_detail = restart_widget_agent()
        pid = _wait_for_pid(launch_agent, result["label"])
    except ClaudeSwitchError as e:
        error(f"Error: {e}")
        if saved:
            error("Your Claude account was saved.")
        elif not_saved is not None:
            error(f"Claude account not saved: {not_saved[0]}")
            error(not_saved[1])
        error("Retry the extra with: openswap menubar --install-service")
        return 1
    except KeyboardInterrupt:
        print(f"\n{dimmed('Operation cancelled')}")
        if saved:
            print(dimmed("Your Claude account was saved."))
        elif not_saved is not None:
            print(dimmed(f"Claude account not saved: {not_saved[0]}"))
            print(dimmed(not_saved[1]))
        return 130

    if pid:
        print(f"Menu bar extra running (pid {pid}). Look for openswap in the menu bar.")
    else:
        warning("Menu bar extra was installed but is not running yet.")
    print(dimmed(f"  log: {result['stderr_log']}"))
    if widget_detail:
        warning(f"Widget host did not restart: {widget_detail}")
        print(dimmed("Run: openswap widget --install"))
    else:
        print(dimmed("Desktop widget (needs Xcode): openswap widget --install"))
    if not_saved is None:
        print(dimmed("Log into another Claude account, then run: openswap add"))
        return 0
    reason, hint, code = not_saved
    warning(f"Claude account not saved: {reason}")
    print(dimmed(hint))
    return code


def _statusline_command(argv: list[str]) -> int:
    """Handle ``openswap statusline`` (paint) and ``--install`` / ``--uninstall``.

    Pre-dispatched so paint never constructs the engine. Always exit 0 on
    paint: a crashed status line is worse than a missing name.
    """
    parser = argparse.ArgumentParser(
        prog=f"{_prog_name()} statusline",
        description=(
            "Opt-in Claude Code status line. Wraps your existing status line "
            "and appends the OpenSwap account name next to the percentages."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--install",
        action="store_true",
        help="Wrap ~/.claude/settings.json statusLine (creates one if missing)",
    )
    group.add_argument(
        "--uninstall",
        action="store_true",
        help="Restore the previous statusLine, or remove one we created",
    )
    args = parser.parse_args(argv)

    from openswap import statusline as sl

    backup = paths.get_backup_root()
    config_home = paths.get_claude_config_home()

    if args.install or args.uninstall:
        try:
            if paths.migrate_legacy_backup_dir(backup):
                print(
                    f"openswap: migrated data from {paths.get_legacy_backup_root()} "
                    f"to {backup}",
                    file=sys.stderr,
                )
        except ClaudeSwitchError as e:
            error(f"Error: {e}")
            return 1

    if args.install:
        try:
            result = sl.install(config_home, backup, command=sl.paint_command())
        except (ClaudeSwitchError, OSError) as e:
            error(f"Error: {e}")
            return 1
        if result.get("already"):
            print("Claude Code status line already wraps OpenSwap.")
            return 0
        if result.get("created"):
            print("Claude Code status line installed (account name only).")
        else:
            print("Claude Code status line now wraps your existing command.")
        print(dimmed("OpenSwap appends the current account name next to the percentages."))
        return 0

    if args.uninstall:
        try:
            result = sl.uninstall(config_home, backup)
        except (ClaudeSwitchError, OSError) as e:
            error(f"Error: {e}")
            return 1
        if result.get("restored"):
            print("Claude Code status line restored.")
        else:
            print("OpenSwap was not wrapping the Claude Code status line.")
        return 0

    try:
        stdin = sys.stdin.read()
        wrap = sl.load_wrap(backup)
        sys.stdout.write(
            sl.paint(
                stdin,
                inner_command=wrap.get("innerCommand"),
                config_path=paths.get_global_config_path(),
                sequence_path=backup / "sequence.json",
            )
        )
    except Exception:
        pass
    return 0


def _menubar_service(args) -> int:
    """Handle ``menubar --install-service|--uninstall-service|--service-status``.

    Split out of the dispatch chain because these three share one import and
    one output shape, and because the menu bar branch below them is a
    non-returning call — folding the service paths inline would leave the
    reader tracing which branches fall through to launching the app.
    """
    from openswap import launch_agent

    if args.install_service:
        result = launch_agent.install()
        print(f"Menu bar service installed ({result['label']}).")
        print(f"  plist: {result['plist']}")
        print(f"  logs:  {result['stderr_log']}")
        print(
            dimmed(
                "It starts at login from now on. Re-run this after a openswap "
                "upgrade to point launchd at the new build."
            )
        )
        return 0

    if args.uninstall_service:
        result = launch_agent.uninstall()
        if result["was_loaded"] or result["removed_plist"]:
            print("Menu bar service removed.")
        else:
            print("Menu bar service was not installed.")
        return 0

    result = launch_agent.status()
    if not result["installed"] and not result["loaded"]:
        print("Menu bar service is not installed.")
        print(dimmed("Install it with: openswap menubar --install-service"))
        return 0
    state = result["state"] or ("loaded" if result["loaded"] else "stopped")
    pid = f" (pid {result['pid']})" if result["pid"] else ""
    print(f"Menu bar service: {state}{pid}")
    print(f"  plist: {result['plist']}")
    if not result["installed"]:
        print(dimmed("launchd still has it loaded, but the plist is gone."))
    return 0


def main() -> None:
    """Main entry point for the CLI."""
    force_utf8_output()
    _use_native_tls()
    argv = sys.argv[1:]
    try:
        from openswap.appearance import cli_should_probe, cli_theme
        # `--json` must stay machine-readable — never probe (and emit the
        # OSC query) in that case.
        probe = cli_should_probe(argv, colors_enabled=printer.colors_enabled())
        name = cli_theme(load_ui_settings(paths.get_backup_root()).theme, colors=probe)
        printer.set_theme(name)
    except Exception:
        pass  # theme is cosmetic; never block the CLI on it

    # `auto` keeps its dedicated pre-dispatch parser.
    if argv and argv[0] == "auto":
        _auto_command(argv[1:])
        return  # only reachable in tests where sys.exit is mocked
    if argv and argv[0] == "codex":
        sys.exit(_codex_command(argv[1:]))
    if argv and argv[0] == "widget":
        sys.exit(_widget_command(argv[1:]))
    if argv and argv[0] == "statusline":
        sys.exit(_statusline_command(argv[1:]))
    if argv and argv[0] == "setup":
        sys.exit(_setup_command(argv[1:]))
    if len(sys.argv) > 1 and sys.argv[1] == "config":
        _config_command(sys.argv[2:])
        return
    if argv and argv[0] == "unclaimed":
        _unclaimed_command(argv[1:])
        return
    if argv and argv[0] == "alias":
        _alias_command(argv[1:])
        return
    if argv and argv[0] == "swap":
        _swap_command(argv[1:])
        return
    if argv and argv[0] == "move":
        _move_command(argv[1:])
        return

    if argv and argv[0] in ("tui", "watch"):
        error(
            "The terminal dashboard is gone. Use the macOS extra "
            "or `openswap list`."
        )
        sys.exit(2)

    if argv and argv[0] in ("run", "map", "unmap"):
        error(
            "Session mode and directory maps are gone. Use the macOS extra "
            "or `openswap list` / `openswap switch`."
        )
        sys.exit(2)

    # Bare `openswap` prints help (used to open the terminal dashboard).
    # The frozen bundle launched by Finder has no terminal: run the extra.
    if _frozen_without_terminal() and not [a for a in argv if not a.startswith("-psn")]:
        argv = ["menubar"]
    if not argv:
        argv = ["--help"]

    # Memorable subcommands (`openswap switch <email>`, `openswap list`, `openswap help`, ...)
    # are rewritten to the equivalent flags so the original `--flag` interface
    # keeps working unchanged.
    argv = _translate_subcommand(argv)

    parser = argparse.ArgumentParser(
        prog=_prog_name(),
        usage="%(prog)s <command> [args] [options]",
        description="""OpenSwap: OpenSoft macOS CLI for rotating Claude Code accounts

Commands:
  %(prog)s help                       show this help
  %(prog)s setup                      save the current login and start the menu bar extra
  %(prog)s list                       list managed accounts
  %(prog)s status                     show current account
  %(prog)s switch                     rotate to the next account
  %(prog)s switch <num|email>         switch to a specific account
  %(prog)s add                        add the current account
  %(prog)s add-token [TOKEN|-]        register an API key or setup-token
  %(prog)s remove <num|email>         remove an account
  %(prog)s disable <num|email>        hold an account out of auto-rotation
  %(prog)s enable <num|email>         return a disabled account to rotation
  %(prog)s alias <num|email> <name>   set a short alias for an account
  %(prog)s alias <num|email> --unset  remove an account's alias
  %(prog)s alias                      list all aliases
  %(prog)s swap <a> <b>               exchange two accounts' slot numbers
  %(prog)s move <a> <slot>            assign an account to a slot (swaps if taken)
  %(prog)s auto                       auto-switch when nearing rate limits
  %(prog)s codex add|list|switch|remove  Codex CLI accounts (second provider)
  %(prog)s config [set KEY VALUE]     show or change shared policy (settings.json)
  %(prog)s unclaimed [--purge ID]     list or drop stashed credential entries
  %(prog)s export <path>              export accounts
  %(prog)s import <path>              import accounts
  %(prog)s menubar                    macOS menu bar extra
  %(prog)s menubar --install-service  keep the extra running via launchd
  %(prog)s widget --install           macOS Desktop / Notification Center widget
  %(prog)s statusline --install       opt-in: wrap Claude Code status line
  %(prog)s purge                      remove all openswap data

Aliases: ls=list  rm=remove""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Flags combine with subcommands:
  %(prog)s switch --strategy best           # pick the account with most quota left
  %(prog)s switch --strategy next-available # rotate, skipping rate-limited accounts
  %(prog)s switch user@example.com
  %(prog)s list --token-status
  %(prog)s list --json
  %(prog)s add --slot 3                      # add to a specific slot
  %(prog)s add-token sk-ant-api03-... --email me@example.com
  %(prog)s add-token sk-ant-oat01-... --email me@example.com
  %(prog)s auto --once                       # single auto-switch tick (cron-friendly)
  %(prog)s config set autoswitch.threshold 80

The original flag spellings (%(prog)s --switch, %(prog)s --list, ...) keep working.
        """,
    )

    # Version and debug flags (outside mutually exclusive group)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--token-status",
        action="store_true",
        help="Show source-labelled OAuth token diagnostics (use with 'list')",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit machine-readable JSON to stdout (use with 'list', 'status', "
            "or 'switch'). See README 'JSON output for scripting'."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=["best", "next-available"],
        metavar="{best,next-available}",
        help=(
            "With bare 'switch': pick the target by remaining 5h/7d quota. "
            "'best' jumps to the account with the most headroom; "
            "'next-available' rotates to the next account, skipping any at their limit"
        ),
    )
    parser.add_argument(
        "--model",
        metavar="NAMES",
        help=(
            "With 'switch --strategy': also count these models' per-model "
            "weekly limits when comparing accounts (comma-separated display "
            "names, or 'all'). Defaults to the autoswitch.model setting"
        ),
    )
    parser.add_argument(
        "--slot",
        type=int,
        metavar="NUM",
        help="Specify slot number when adding account (use with 'add' or 'add-token')",
    )
    parser.add_argument(
        "--email",
        metavar="EMAIL",
        help=(
            "Email address for the account. Optional with 'add-token'; "
            "defaults to setup-token-{slot}@token.local (or "
            "api-key-{slot}@token.local for API keys) since these tokens "
            "carry no real email metadata."
        ),
    )
    parser.add_argument(
        "--account",
        metavar="NUM|EMAIL",
        help="Limit export to one account (use with 'export')",
    )
    parser.add_argument(
        "--alias",
        metavar="NAME",
        help="Set a short display alias for the account (use with 'add')",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite existing accounts during import; with 'switch <num|email>', "
            "activate the stored credentials without backing up the current "
            "login first"
        ),
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include full ~/.claude.json in export (default: oauthAccount only)",
    )
    parser.add_argument(
        "--install-service",
        action="store_true",
        help=(
            "With 'menubar': install a launchd LaunchAgent so the menu bar "
            "starts at login and restarts on crash (macOS)"
        ),
    )
    parser.add_argument(
        "--uninstall-service",
        action="store_true",
        help="With 'menubar': stop the LaunchAgent and remove its plist (macOS)",
    )
    parser.add_argument(
        "--service-status",
        action="store_true",
        help=(
            "With 'menubar': report whether the LaunchAgent is installed "
            "and running"
        ),
    )

    # Legacy `--flag` interface. Still fully supported (bare subcommands rewrite
    # into these, see _translate_subcommand), but hidden from --help so the
    # subcommands shown in the description are the one documented interface.
    # The group is not `required` because the "no command" case is handled
    # explicitly below (a required group with every member suppressed prints a
    # broken empty-list error).
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "--add-account",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--remove-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--disable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--enable-account",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--list",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--switch-to",
        metavar="NUM|EMAIL",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--status",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--purge",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--export",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--import",
        dest="import_",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--menubar",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--upgrade",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    group.add_argument(
        "--add-token",
        metavar="TOKEN|-",
        nargs="?",
        const="",
        help=argparse.SUPPRESS,
    )

    args = parser.parse_args(argv)

    # No action selected: emit a clean, subcommand-oriented message rather than
    # the raw argparse "one of the arguments ... is required" (which would list
    # the now-hidden legacy flags). Value actions can be falsy-but-set
    # (--add-token uses const=""), so test those with `is not None`.
    if not (
        args.add_account
        or args.list
        or args.switch
        or args.status
        or args.purge
        or args.menubar
        or args.upgrade
        or args.remove_account is not None
        or args.disable_account is not None
        or args.enable_account is not None
        or args.switch_to is not None
        or args.export is not None
        or args.import_ is not None
        or args.add_token is not None
    ):
        parser.error("no command given — try '%(prog)s help'" % {"prog": _prog_name()})

    if args.token_status and not args.list:
        parser.error("--token-status can only be used with 'list'")

    if args.json and not (args.list or args.status or args.switch or args.switch_to):
        parser.error("--json can only be used with 'list', 'status', or 'switch'")

    if args.json and args.token_status:
        # Token status is not part of the JSON v1 schema; reject rather than
        # silently ignore it (a future additive field can add it).
        parser.error("--token-status cannot be combined with --json")

    if args.strategy is not None and not args.switch:
        parser.error("--strategy can only be used with bare 'switch'")

    if args.model is not None and args.strategy is None:
        # Meaningless on a direct-target switch or plain rotation — nothing
        # usage-aware reads it there, so reject loudly rather than ignore.
        parser.error(
            "--model can only be used with 'switch --strategy best' or "
            "'switch --strategy next-available'"
        )

    if args.slot is not None and not (args.add_account or args.add_token is not None):
        parser.error("--slot can only be used with 'add' or 'add-token'")

    if args.email is not None and args.add_token is None:
        parser.error("--email can only be used with 'add-token'")

    if args.account is not None and not args.export:
        parser.error("--account can only be used with 'export'")

    if args.alias is not None and not args.add_account:
        parser.error("--alias can only be used with 'add'")

    if args.force and not (args.import_ or args.switch_to):
        parser.error("--force can only be used with 'import' or 'switch <num|email>'")

    if args.full and not args.export:
        parser.error("--full can only be used with 'export'")

    if (
        args.install_service or args.uninstall_service or args.service_status
    ) and not args.menubar:
        parser.error(
            "--install-service, --uninstall-service and --service-status "
            "can only be used with 'menubar'"
        )

    # Self-upgrade runs before switcher init so we don't touch config/keychain
    # just to upgrade the tool itself.
    if args.upgrade:
        from openswap.update_check import run_self_upgrade

        try:
            sys.exit(run_self_upgrade())
        except KeyboardInterrupt:
            print(f"\n{dimmed('Upgrade cancelled')}")
            sys.exit(130)

    # Initialize switcher and dispatch under a single error handler so
    # init-time failures (e.g. MigrationError on a backup-dir collision)
    # are presented like every other ClaudeSwitchError: clean stderr line,
    # exit 1, no traceback.
    # JSON-capable commands return a payload; the CLI is the single point that
    # serializes it (so no command writes JSON to stdout itself).
    payload: dict | None = None
    try:
        switcher = ClaudeAccountSwitcher(debug=args.debug)

        # Check for root (unless in container) - POSIX only
        if sys.platform != "win32":
            if os.geteuid() == 0 and not switcher._is_running_in_container():
                error("Error: Do not run this script as root (unless running in a container)")
                sys.exit(1)

        if args.add_account:
            switcher.add_account(slot=args.slot, alias=args.alias)
        elif args.add_token is not None:
            switcher.add_account_from_token(
                token=args.add_token,
                email=args.email,
                slot=args.slot,
            )
        elif args.remove_account:
            switcher.remove_account(args.remove_account)
        elif args.disable_account is not None:
            switcher.set_account_disabled(args.disable_account, True)
        elif args.enable_account is not None:
            switcher.set_account_disabled(args.enable_account, False)
        elif args.list:
            payload = switcher.list_accounts(
                show_token_status=args.token_status,
                json_output=args.json,
            )
            if not args.json:
                from openswap.codex.engine import CodexEngine

                codex = CodexEngine(debug=args.debug)
                if codex.accounts_snapshot(fetch=set()).accounts:
                    print()
                    print(accent("Codex"))
                    codex.list_accounts()
        elif args.switch:
            from openswap.settings import load_settings, parse_model_names

            # Only the usage-aware strategies read model limits: --model wins;
            # otherwise the persistent autoswitch.model setting applies
            # (announced by switch(), never silently).
            if args.strategy is None:
                models, model_source = (), None
            elif args.model is not None:
                models, model_source = parse_model_names(args.model), "cli"
            else:
                models = parse_model_names(load_settings(switcher.backup_dir).model)
                model_source = "autoswitch.model" if models else None
            payload = switcher.switch(
                strategy=args.strategy,
                json_output=args.json,
                models=models,
                model_source=model_source,
            )
            if payload is not None and models:
                payload["models"] = list(models)
                payload["modelSource"] = model_source
        elif args.switch_to:
            payload = switcher.switch_to(
                args.switch_to, json_output=args.json, force=args.force
            )
        elif args.status:
            payload = switcher.status(json_output=args.json)
        elif args.purge:
            switcher.purge()
        elif args.export:
            from openswap.transfer import export_accounts

            export_accounts(switcher, args.export, account=args.account, full=args.full)
        elif args.import_:
            from openswap.transfer import import_accounts

            import_accounts(switcher, args.import_, force=args.force)
        elif args.menubar:
            if sys.platform != "darwin":
                error("The menu bar is only available on macOS.")
                sys.exit(1)
            if args.install_service or args.uninstall_service or args.service_status:
                sys.exit(_menubar_service(args))
            # menubar is import-safe without the extra; a missing rumps
            # surfaces from run() as a ClaudeSwitchError with the install hint.
            from openswap.codex.engine import CodexEngine
            from openswap.menubar import run as menubar_run

            sys.exit(menubar_run(switcher, codex=CodexEngine()))
    except ClaudeSwitchError as e:
        # In JSON mode keep stdout pure JSON: emit the structured error envelope
        # there (exit 1) instead of a red stderr line.
        if args.json:
            print(json.dumps(error_envelope(e), indent=2))
        else:
            error(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        # Route the cancellation note to stderr in JSON mode so stdout stays
        # parseable (the guarantee covers completion / handled errors, not Ctrl-C).
        print(
            f"\n{dimmed('Operation cancelled')}",
            file=sys.stderr if args.json else sys.stdout,
        )
        sys.exit(130)

    if args.json and payload is not None:
        print(json.dumps(payload, indent=2))

    # Passive update notification (never fails). Skipped after --purge so we
    # don't immediately recreate <backup_root>/cache/update_check.json inside
    # the directory we just deleted. Skipped after --upgrade as a safety guard
    # in case the dispatch is later refactored to fall through.
    if not args.purge and not args.upgrade and not args.json:
        from openswap.update_check import check_for_update

        msg = check_for_update(__version__)
        if msg:
            print(f"\n{muted(msg)}", file=sys.stderr)


if __name__ == "__main__":
    main()
