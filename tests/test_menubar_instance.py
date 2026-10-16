"""One menu bar extra at a time (plan 020, Step 1).

The lock is a real ``flock`` on a temp file: flock belongs to the open file
description, so two opens in this one process conflict exactly as two
processes would. Foundation, rumps and AppKit are faked; nothing here posts a
real distributed notification or starts a status item.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from openswap import launch_agent, menubar, menubar_instance
from openswap.exceptions import ClaudeSwitchError
from openswap.menubar_instance import (
    ALREADY_RUNNING,
    LOGIN_ITEM_WAIT_SECONDS,
    SHOW_NOTIFICATION,
    AlreadyRunning,
    claim,
    consume_show_request,
    hand_over_to_login_item,
    lock_path,
    menubar_lock,
    leave_show_request,
    show_request_path,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the extra is macOS-only (flock)")


@pytest.fixture(autouse=True)
def _instant_waits(monkeypatch):
    """Lock waits and the holder watch run on a clock that moves only when
    the code sleeps, so they cost no real time."""
    clock = [0.0]
    monkeypatch.setattr(menubar_instance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(menubar_instance.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return clock


def test_the_lock_lives_in_the_data_folder(tmp_path):
    assert lock_path(tmp_path) == tmp_path / "menubar.lock"


def test_two_holders_conflict_even_inside_one_process(tmp_path):
    first = menubar_lock(tmp_path)
    assert first is not None
    try:
        assert menubar_lock(tmp_path) is None
    finally:
        first.release()
    again = menubar_lock(tmp_path)
    assert again is not None
    again.release()


def test_the_holder_writes_its_pid_and_a_losing_copy_leaves_it(tmp_path):
    held = menubar_lock(tmp_path)
    try:
        assert menubar_instance.lock_holder_pid(tmp_path) == os.getpid()
        assert menubar_lock(tmp_path) is None  # opening it to try must not truncate it
        assert menubar_instance.lock_holder_pid(tmp_path) == os.getpid()
    finally:
        held.release()


@pytest.mark.parametrize("text", [None, "", "not a pid\n"])
def test_no_holder_pid_without_a_readable_one(tmp_path, text):
    if text is not None:
        lock_path(tmp_path).write_text(text)
    assert menubar_instance.lock_holder_pid(tmp_path) is None


def test_process_alive():
    assert menubar_instance.process_alive(os.getpid()) is True
    done = subprocess.run(["true"], check=True)
    assert done.returncode == 0
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert menubar_instance.process_alive(gone.pid) is False


def test_a_free_lock_is_claimed_without_asking_anyone(tmp_path):
    posted, said = [], []
    lock = claim(tmp_path, as_login_item=False, post=lambda: posted.append(1), say=said.append)
    try:
        assert lock is not None
        assert lock_path(tmp_path).exists()
        assert posted == [] and said == []
    finally:
        lock.release()


def test_another_copy_does_not_wait_it_asks_the_running_one_to_show(tmp_path, monkeypatch):
    held = menubar_lock(tmp_path)
    waits = []
    real = menubar_instance.menubar_lock
    monkeypatch.setattr(
        menubar_instance, "menubar_lock",
        lambda backup_dir, wait=0.0: waits.append(wait) or real(backup_dir, wait),
    )
    posted, said = [], []
    try:
        with pytest.raises(AlreadyRunning) as raised:
            claim(
                tmp_path, as_login_item=False, say=said.append,
                post=lambda: posted.append(show_request_path(tmp_path).exists()),
            )
    finally:
        held.release()
    assert raised.value.exit_code == 0
    # Never a wait for the lock itself, only quick retries while watching.
    assert set(waits) == {0.0}
    # A show request: the file is written before the notification goes out.
    assert posted == [True]
    assert said == [ALREADY_RUNNING]
    assert ALREADY_RUNNING == "OpenSwap is already running in the menu bar."


def test_a_holder_that_takes_the_show_request_lets_this_copy_go_at_once(tmp_path, _instant_waits):
    held = menubar_lock(tmp_path)
    try:
        with pytest.raises(AlreadyRunning) as raised:
            claim(
                tmp_path, as_login_item=False, say=lambda _m: None,
                # The holder's notification handler consumes the request.
                post=lambda: menubar_instance.consume_show_request(tmp_path),
            )
    finally:
        held.release()
    assert raised.value.exit_code == 0
    assert _instant_waits[0] < menubar_instance.HOLDER_ANSWER_SECONDS


def test_a_holder_that_quits_meanwhile_leaves_the_menu_bar_to_this_copy(tmp_path):
    """It was between Quit and exit: it never answers, and its clean exit is
    not restarted, so this copy must run rather than leave nothing."""
    held = menubar_lock(tmp_path)
    said = []
    lock = claim(tmp_path, as_login_item=False, say=said.append, post=held.release)
    assert lock is not None
    try:
        assert menubar_instance.lock_holder_pid(tmp_path) == os.getpid()
        assert said == []
        # Its own request opens its popover once it attaches.
        assert show_request_path(tmp_path).exists()
    finally:
        lock.release()


def test_a_holder_that_stays_silent_is_given_two_seconds(tmp_path, _instant_waits):
    held = menubar_lock(tmp_path)
    try:
        with pytest.raises(AlreadyRunning) as raised:
            claim(tmp_path, as_login_item=False, say=lambda _m: None, post=lambda: None)
    finally:
        held.release()
    assert raised.value.exit_code == 0
    assert menubar_instance.HOLDER_ANSWER_SECONDS == 2.0
    assert _instant_waits[0] >= menubar_instance.HOLDER_ANSWER_SECONDS


def test_the_launchd_job_waits_then_exits_1_for_keepalive_to_retry(tmp_path, monkeypatch, capsys):
    waits = []
    monkeypatch.setattr(
        menubar_instance, "menubar_lock",
        lambda backup_dir, wait=0.0: waits.append(wait) or None,
    )
    posted = []
    with pytest.raises(AlreadyRunning) as raised:
        claim(
            tmp_path, as_login_item=True, post=lambda: posted.append(1), say=print,
            unload=lambda: False,
        )
    assert raised.value.exit_code == 1
    assert waits == [LOGIN_ITEM_WAIT_SECONDS] and LOGIN_ITEM_WAIT_SECONDS == 10.0
    # It is the copy that should run, so it never asks the other one to show.
    assert posted == []
    out = capsys.readouterr()
    assert ALREADY_RUNNING not in out.out
    assert "launchd will retry" in out.err


def test_a_job_whose_login_item_was_turned_off_unloads_itself_and_exits_0(tmp_path, monkeypatch, capsys):
    """Exiting 1 would have KeepAlive restart it behind the other copy forever."""
    monkeypatch.setattr(menubar_instance, "menubar_lock", lambda backup_dir, wait=0.0: None)
    unloaded = []
    with pytest.raises(AlreadyRunning) as raised:
        claim(
            tmp_path, as_login_item=True, post=lambda: None, say=print,
            unload=lambda: unloaded.append(1) or True,
        )
    assert raised.value.exit_code == 0
    assert unloaded == [1]
    assert "Open at login is off" in capsys.readouterr().err


def test_another_copy_never_unloads_the_job(tmp_path):
    held = menubar_lock(tmp_path)
    unloaded = []
    try:
        with pytest.raises(AlreadyRunning):
            claim(
                tmp_path, as_login_item=False, post=lambda: None, say=lambda _m: None,
                unload=lambda: unloaded.append(1) or True,
            )
    finally:
        held.release()
    assert unloaded == []


def test_the_launchd_job_gets_the_lock_once_the_old_copy_quits(tmp_path):
    held = menubar_lock(tmp_path)
    held.release()  # the handing-over copy quit
    lock = claim(tmp_path, as_login_item=True, post=lambda: None, say=print)
    assert lock is not None
    lock.release()


def test_a_post_failure_still_exits_0(tmp_path):
    held = menubar_lock(tmp_path)

    def boom():
        raise RuntimeError("no window server")

    try:
        with pytest.raises(AlreadyRunning) as raised:
            claim(tmp_path, as_login_item=False, post=boom, say=lambda _m: None)
    finally:
        held.release()
    assert raised.value.exit_code == 0


def test_an_unusable_lock_file_runs_unguarded_rather_than_not_at_all(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    logged = []
    logger = types.SimpleNamespace(warning=lambda *a, **k: logged.append(a[0]))
    # The data folder is a file: the lock file cannot be created under it.
    assert claim(blocker, as_login_item=False, post=lambda: None, say=print, logger=logger) is None
    assert logged and "running without it" in logged[0]


def test_the_show_request_is_the_named_distributed_notification(monkeypatch):
    posts = []

    class Center:
        def postNotificationName_object_userInfo_deliverImmediately_(self, *args):
            posts.append(args)

    foundation = types.ModuleType("Foundation")
    foundation.NSDistributedNotificationCenter = types.SimpleNamespace(defaultCenter=Center)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)

    menubar_instance.post_show_notification()

    assert SHOW_NOTIFICATION == "com.opensoft.openswap.menubar.show"
    assert posts == [(SHOW_NOTIFICATION, None, None, True)]


# --- handing over to an idle login item ---------------------------------------


@pytest.fixture
def login_item(monkeypatch):
    """launch_agent as a copy launchd did not start sees it; records the order."""
    state = types.SimpleNamespace(runs_this=True, running_pid=None, pid=4242, raises=None, events=[])

    def runs_this(home=None):
        state.events.append("runs_this")
        return state.runs_this

    def running_pid(*_a, **_k):
        state.events.append("status")
        return state.running_pid

    def start(*, home=None, uid=None, backup_dir=None):
        state.events.append("start")
        if state.raises is not None:
            raise state.raises
        return state.pid

    monkeypatch.setattr(launch_agent, "login_item_runs_this", runs_this)
    monkeypatch.setattr(launch_agent, "running_login_item_pid", running_pid)
    monkeypatch.setattr(launch_agent, "start_login_item", start)
    monkeypatch.setattr(launch_agent, "log_paths", lambda *a, **k: (Path("/L/o.log"), Path("/L/e.err")))
    return state


def _logger(logged):
    return types.SimpleNamespace(warning=lambda msg, *args, **k: logged.append(msg % args))


def test_an_idle_login_item_that_runs_this_copy_takes_over(tmp_path, login_item):
    said = []
    post = lambda: login_item.events.append(  # noqa: E731
        "posted" if show_request_path(tmp_path).exists() else "posted without the file"
    )
    assert hand_over_to_login_item(tmp_path, post=post, say=said.append) is True
    # The show request is on disk before the job can start and look for it.
    assert login_item.events == ["runs_this", "posted", "status", "start"]
    assert show_request_path(tmp_path).exists()
    assert said == ["Started OpenSwap from its login item (pid 4242).\n  log: /L/e.err"]


def test_a_login_item_already_running_is_not_started_again(tmp_path, login_item):
    """Starting up, or waiting for this copy's lock: it takes over on its own."""
    login_item.running_pid = 777
    said = []
    assert hand_over_to_login_item(tmp_path, post=lambda: None, say=said.append) is True
    assert "start" not in login_item.events
    assert show_request_path(tmp_path).exists()
    assert said == ["Started OpenSwap from its login item (pid 777).\n  log: /L/e.err"]


def test_no_hand_over_when_login_is_off_or_runs_another_program(tmp_path, login_item):
    login_item.runs_this = False
    posted = []
    assert hand_over_to_login_item(tmp_path, post=lambda: posted.append(1), say=print) is False
    assert login_item.events == ["runs_this"]
    assert posted == []
    assert not show_request_path(tmp_path).exists()


@pytest.mark.parametrize("pid, raises", [(None, None), (None, ClaudeSwitchError("launchctl bootstrap failed"))])
def test_a_hand_over_that_fails_is_logged_and_this_copy_runs(tmp_path, login_item, pid, raises):
    login_item.pid, login_item.raises = pid, raises
    logged = []
    assert hand_over_to_login_item(tmp_path, post=lambda: None, say=print, logger=_logger(logged)) is False
    assert logged and "running here" in logged[-1]


def test_a_show_request_is_consumed_once_whichever_path_gets_there_first(tmp_path):
    leave_show_request(tmp_path, post=lambda: None)
    # The notification handler (or start-up) takes it ...
    assert consume_show_request(tmp_path) is True
    assert not show_request_path(tmp_path).exists()
    # ... and the other path finds nothing, so the popover opens once.
    assert consume_show_request(tmp_path) is False


def test_a_stale_show_request_is_dropped_without_showing(tmp_path):
    leave_show_request(tmp_path, post=lambda: None)
    later = show_request_path(tmp_path).stat().st_mtime + 31
    assert consume_show_request(tmp_path, now=later) is False
    assert not show_request_path(tmp_path).exists()


def test_a_show_request_still_posts_when_the_file_cannot_be_written(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    posted, logged = [], []
    leave_show_request(blocker, post=lambda: posted.append(1), logger=_logger(logged))
    assert posted == [1]
    assert logged == ["could not write the show request"]


# --- the CLI's hand-over request ----------------------------------------------


def test_a_handoff_request_is_a_file_then_a_notification_read_once(tmp_path):
    posted = []
    menubar_instance.leave_handoff_request(
        tmp_path, post=lambda: posted.append(menubar_instance.handoff_request_path(tmp_path).exists())
    )
    assert posted == [True]
    assert menubar_instance.consume_handoff_request(tmp_path) is True
    assert menubar_instance.consume_handoff_request(tmp_path) is False
    menubar_instance.leave_handoff_request(tmp_path, post=lambda: None)
    later = menubar_instance.handoff_request_path(tmp_path).stat().st_mtime + 31
    assert menubar_instance.consume_handoff_request(tmp_path, now=later) is False


def test_the_handoff_notification_name(monkeypatch):
    posts = []

    class Center:
        def postNotificationName_object_userInfo_deliverImmediately_(self, *args):
            posts.append(args)

    foundation = types.ModuleType("Foundation")
    foundation.NSDistributedNotificationCenter = types.SimpleNamespace(defaultCenter=Center)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    menubar_instance.post_handoff_notification()
    assert menubar_instance.HANDOFF_NOTIFICATION == "com.opensoft.openswap.menubar.handoff"
    assert posts == [(menubar_instance.HANDOFF_NOTIFICATION, None, None, True)]


@pytest.fixture
def fake_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(menubar_instance.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(menubar_instance.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    return clock


def test_the_cli_waits_until_launchds_copy_holds_the_lock(tmp_path, monkeypatch, fake_clock):
    holders = iter([999, 999, None, 4242])
    monkeypatch.setattr(menubar_instance, "lock_holder_pid", lambda backup_dir: next(holders))
    posted = []
    assert menubar_instance.hand_over_from_cli(tmp_path, 4242, post=lambda: posted.append(1)) == 4242
    assert posted == [1]
    assert menubar_instance.handoff_request_path(tmp_path).exists()
    assert fake_clock[0] < menubar_instance.CLI_HANDOFF_WAIT_SECONDS


def test_the_cli_gives_up_after_15_seconds_and_names_the_holder(tmp_path, monkeypatch, fake_clock):
    monkeypatch.setattr(menubar_instance, "lock_holder_pid", lambda backup_dir: 999)
    assert menubar_instance.hand_over_from_cli(tmp_path, 4242, post=lambda: None) == 999
    assert fake_clock[0] >= menubar_instance.CLI_HANDOFF_WAIT_SECONDS == 15.0


def test_a_real_holder_is_seen_by_the_cli(tmp_path, fake_clock):
    held = menubar_lock(tmp_path)
    try:
        assert menubar_instance.hand_over_from_cli(tmp_path, os.getpid(), post=lambda: None) == os.getpid()
    finally:
        held.release()


@pytest.mark.parametrize(
    "as_job, runs_this, pid, expected",
    [
        (False, True, 4242, 4242),  # step aside
        (True, True, 4242, None),  # the job never steps aside for itself
        (False, False, 4242, None),  # the login item runs another program
        (False, True, None, None),  # launchd's copy is not up
    ],
)
def test_login_item_waiting(monkeypatch, as_job, runs_this, pid, expected):
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: as_job)
    monkeypatch.setattr(launch_agent, "login_item_runs_this", lambda home=None: runs_this)
    monkeypatch.setattr(launch_agent, "running_login_item_pid", lambda **k: pid)
    assert menubar_instance.login_item_waiting() == expected


def test_login_item_waiting_logs_a_failed_check(monkeypatch):
    def boom(home=None):
        raise OSError("launchctl missing")

    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: False)
    monkeypatch.setattr(launch_agent, "login_item_runs_this", boom)
    logged = []
    assert menubar_instance.login_item_waiting(logger=_logger(logged)) is None
    assert logged == ["could not check the login item"]


# --- menubar.run --------------------------------------------------------------


def _fake_gui(monkeypatch):
    """rumps and AppKit stand-ins that fail the test if the GUI starts, and a
    Foundation that records show notifications. Returns those posts."""

    def no_gui(*_a, **_k):
        raise AssertionError("the status item was set up despite another extra")

    posts = []

    class Center:
        def postNotificationName_object_userInfo_deliverImmediately_(self, *args):
            posts.append(args[0])

    foundation = types.ModuleType("Foundation")
    foundation.NSDistributedNotificationCenter = types.SimpleNamespace(defaultCenter=Center)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)

    appkit = types.ModuleType("AppKit")
    appkit.NSApplication = types.SimpleNamespace(sharedApplication=no_gui)
    appkit.NSApplicationActivationPolicyAccessory = 1
    rumps = types.ModuleType("rumps")
    rumps.App = type("App", (), {"__init__": no_gui})
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "rumps", rumps)
    monkeypatch.setattr(menubar, "ensure_notification_identity", lambda: None)
    return posts


@pytest.mark.parametrize("as_login_item, code", [(False, 0), (True, 1)])
def test_run_exits_before_any_status_item_when_another_extra_runs(
    tmp_path, monkeypatch, login_item, as_login_item, code
):
    _fake_gui(monkeypatch)
    seen = {}

    def fake_claim(backup_dir, *, as_login_item, logger=None, **_k):
        seen.update(backup_dir=backup_dir, as_login_item=as_login_item)
        raise AlreadyRunning(1 if as_login_item else 0)

    monkeypatch.setattr(menubar_instance, "claim", fake_claim)
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: as_login_item)
    switcher = types.SimpleNamespace(backup_dir=tmp_path, _logger=None)

    assert menubar.run(switcher) == code
    assert seen == {"backup_dir": tmp_path, "as_login_item": as_login_item}
    # A running login item holds the lock: it is told to show itself (the
    # posted notification), never started again.
    assert "start" not in login_item.events


def test_a_running_login_item_is_asked_to_show_itself_and_never_started(
    tmp_path, monkeypatch, login_item, capsys
):
    """It holds the lock, so this copy leaves a show request and exits 0."""
    posts = _fake_gui(monkeypatch)
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: False)
    held = menubar_lock(tmp_path)  # the running launchd copy
    try:
        assert menubar.run(types.SimpleNamespace(backup_dir=tmp_path, _logger=None)) == 0
    finally:
        held.release()
    assert posts == [SHOW_NOTIFICATION]
    assert show_request_path(tmp_path).exists()
    assert "start" not in login_item.events
    assert ALREADY_RUNNING in capsys.readouterr().out


def test_run_hands_over_to_an_idle_login_item_and_lets_go_of_the_lock(
    tmp_path, monkeypatch, login_item, capsys
):
    _fake_gui(monkeypatch)
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: False)
    switcher = types.SimpleNamespace(backup_dir=tmp_path, _logger=None)

    assert menubar.run(switcher) == 0
    assert login_item.events[-1] == "start"
    assert show_request_path(tmp_path).exists()  # for the job's popover
    assert "Started OpenSwap from its login item" in capsys.readouterr().out
    # The job can take the lock now.
    lock = menubar_lock(tmp_path)
    assert lock is not None
    lock.release()


def test_run_steps_aside_for_a_starting_login_item_without_starting_it_again(
    tmp_path, monkeypatch, login_item, capsys
):
    """This copy won the lock, but launchd's copy has a pid: it is starting
    (an app open racing login or a kickstart) and waits for the lock."""
    posts = _fake_gui(monkeypatch)
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: False)
    login_item.running_pid = 777

    assert menubar.run(types.SimpleNamespace(backup_dir=tmp_path, _logger=None)) == 0
    assert "start" not in login_item.events
    # A show request for the waiting copy, which consumes it once attached.
    assert posts == [SHOW_NOTIFICATION]
    assert show_request_path(tmp_path).exists()
    assert "pid 777" in capsys.readouterr().out
    # The lock is free for it.
    lock = menubar_lock(tmp_path)
    assert lock is not None
    lock.release()


def test_a_failed_hand_over_unloads_the_job_and_this_copy_runs_on(tmp_path, monkeypatch):
    """launchd accepts the kickstart but throttles a job that crashed recently,
    so no pid appears. Left loaded, the delayed start would come up later,
    wait behind this copy's lock and loop under KeepAlive."""
    from tests.test_launch_agent import FakeLaunchd

    program = ["/Users/x/.local/bin/openswap"]
    home, data = tmp_path / "home", tmp_path / "data"
    launchd = FakeLaunchd(loaded=True, spawns=False)
    launchd.loaded_program = [*program, "menubar"]  # loaded from this plist: kickstart
    clock = [1000.0]
    monkeypatch.setattr(launch_agent.sys, "platform", "darwin")
    monkeypatch.setattr(launch_agent.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(launch_agent.subprocess, "run", launchd)
    monkeypatch.setattr(launch_agent, "resolve_program", lambda: list(program))
    monkeypatch.setattr(launch_agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launch_agent.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: False)
    plist = launch_agent.plist_path()
    plist.parent.mkdir(parents=True)
    plist.write_bytes(launch_agent.build_plist(program))
    _fake_gui(monkeypatch)

    # This copy carries on: past the lock and the hand-over, into the GUI.
    with pytest.raises(AssertionError, match="status item was set up"):
        menubar.run(types.SimpleNamespace(backup_dir=data, _logger=None))

    assert "kickstart" in launchd.subcommands
    assert launchd.subcommands[-1] == "bootout"
    assert launchd.loaded is False
    assert plist.exists()  # login still starts it next time
    failure = launch_agent.last_start_failure(data)
    assert failure is not None and "no process after launchctl kickstart" in failure["detail"]


def test_the_login_item_itself_never_hands_over_and_clears_a_failed_start(
    tmp_path, monkeypatch, login_item
):
    _fake_gui(monkeypatch)
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: True)
    launch_agent._record_start_failure("launchctl bootstrap failed", tmp_path)
    switcher = types.SimpleNamespace(backup_dir=tmp_path, _logger=None)
    # It gets the lock and goes on to start the GUI, which the fake refuses.
    with pytest.raises(AssertionError, match="status item was set up"):
        menubar.run(switcher)
    assert login_item.events == []
    # launchd started it, so Settings stops warning about the last failure.
    assert launch_agent.last_start_failure(tmp_path) is None


def test_another_copy_leaves_a_failed_start_for_settings(tmp_path, monkeypatch, login_item):
    login_item.runs_this = False
    _fake_gui(monkeypatch)
    monkeypatch.setattr(launch_agent, "runs_as_login_item", lambda: False)
    launch_agent._record_start_failure("launchctl bootstrap failed", tmp_path)
    with pytest.raises(AssertionError, match="status item was set up"):
        menubar.run(types.SimpleNamespace(backup_dir=tmp_path, _logger=None))
    assert launch_agent.last_start_failure(tmp_path) is not None


def test_run_takes_the_lock_before_the_app_and_its_engines_exist():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    run = text[text.index("def run(") : text.index("class MenuBarApp")]
    assert run.index("claim(") < run.index("setActivationPolicy_")
