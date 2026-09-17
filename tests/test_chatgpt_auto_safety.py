"""The desktop candidate monitor must stop before a real auth commit."""

from unittest.mock import Mock

from openswap.autoswitch import AutoSwitchEngine, TickOutcome
from openswap.settings import AutoSwitchSettings, set_setting
from tests.test_autoswitch import FakeClock, _entry_for, _usage, codex_harness


def test_codex_monitor_proposes_without_switching_or_refreshing_tokens(tmp_path, monkeypatch):
    clock = FakeClock()
    codex, home = codex_harness(tmp_path, clock)
    set_setting(codex.backup_dir, "autoswitch.codexEnabled", "false")
    live_before = (home / "auth.json").read_bytes()
    roster_before = codex.sequence_file.read_bytes()
    slots_before = {num: codex._slot_text(num) for num in ("1", "2")}
    entries = {"1": _entry_for(_usage(95), clock()), "2": _entry_for(_usage(10), clock())}
    monkeypatch.setattr(codex, "usage_entries_by_account", lambda *a, **kw: entries)
    switch = Mock(side_effect=AssertionError("monitor must not switch"))
    freshen = Mock(side_effect=AssertionError("monitor must not freshen tokens"))
    monkeypatch.setattr(codex, "switch_to", switch)
    monkeypatch.setattr(codex, "freshen_backup", freshen)
    events = []
    state_path = codex.state_dir / "desktop-monitor-test.json"
    engine = AutoSwitchEngine(codex, AutoSwitchSettings(), events.append,
                              dry_run=True, state_path=state_path, clock=clock)

    assert engine.tick() is TickOutcome.SWITCHED
    proposal = next(event for event in events if event.kind == "switch")
    assert proposal.dry_run is True
    assert str(proposal.to_ref["number"]) == "2"
    assert proposal.provider == "codex"
    switch.assert_not_called()
    freshen.assert_not_called()
    assert (home / "auth.json").read_bytes() == live_before
    assert codex.sequence_file.read_bytes() == roster_before
    assert {num: codex._slot_text(num) for num in ("1", "2")} == slots_before
    assert not state_path.exists()
