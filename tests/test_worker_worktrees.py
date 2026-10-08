"""Work folders: where a remote Claude or Codex session is launched and works.

Real git in temp folders: a repo becomes a work folder, a folder of repos
offers each repo, each task gets its own worktree on an ``openswap/`` branch,
finished tasks' clean worktrees are removed and their branches kept. On macOS
the generated Seatbelt profile is run with ``sandbox-exec`` to check the
write scope: a commit in the worktree works, writing outside it does not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from openswap.settings import WorkerWorkspace, configure_worker_service, load_worker_settings, set_worker_workspaces
from openswap.worker import cli, worktrees
from openswap.worker.runtime import WorkerRuntime, WorkspaceRefused
from tests.test_worker_accounts import root  # noqa: F401 (fixture)
from tests.test_worker_core import _FakeAdapter, _submission

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home"
    folder.mkdir()
    os.chmod(folder, 0o755)
    monkeypatch.setattr(cli, "home_folder", lambda: folder)
    monkeypatch.setattr(cli, "default_research_folder", lambda: folder / "OpenSwap Research")
    return folder


def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Owner", "GIT_AUTHOR_EMAIL": "owner@example.com",
           "GIT_COMMITTER_NAME": "Owner", "GIT_COMMITTER_EMAIL": "owner@example.com"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def _repo(path: Path, *, commit: bool = True) -> Path:
    path.mkdir(parents=True)
    for folder in [path, *path.parents]:
        if folder.name == "home":
            break
        os.chmod(folder, 0o755)
    _git(path, "init", "-q", "-b", "main")
    if commit:
        (path / "README.md").write_text("hello\n")
        _git(path, "add", "README.md")
        _git(path, "commit", "-q", "-m", "first")
    return path


def _runtime(root, *, running=True):
    runtime = WorkerRuntime(root, adapter=_FakeAdapter())
    if running:
        # These tasks have no journal record; keep them running so the next
        # launch's sweep leaves their worktrees alone.
        runtime._job_finished = lambda _job_id: False
    return runtime


def _ids(root):
    return [w.workspace_id for w in cli.launchable_workspaces(root, load_worker_settings(root).workspaces)]


# --- what can be chosen -------------------------------------------------------------------------


def test_a_repo_becomes_one_work_folder(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    result = cli.add_work_folder(root, repo)
    workspace = result.workspace
    assert result.added and (workspace.workspace_id, workspace.display_label) == ("openswap", "openswap")
    assert workspace.work_root == repo.resolve() and workspace.mode == "worktree" and not workspace.repos
    assert workspace.output_root == (home / "OpenSwap Research" / "openswap").resolve()
    assert workspace.readonly_roots == ()
    assert cli.add_work_folder(root, repo).added is False  # the same folder again changes nothing


def test_a_folder_of_repos_offers_each_repo_and_rescans(root, home):
    github = home / "GitHub"
    _repo(github / "opentag")
    _repo(github / "OpenSwap")
    (github / "notes").mkdir()  # not a repo: not offered
    result = cli.add_work_folder(root, github)
    assert result.workspace.repos and result.repos == ("openswap", "opentag")
    # The folder of repos itself is never a place a task can name; its repos are.
    assert _ids(root) == ["openswap", "opentag"]
    launchable = cli.launchable_workspaces(root, load_worker_settings(root).workspaces)
    assert [w.display_label for w in launchable] == ["OpenSwap", "opentag"]
    # A repo added later appears on the next scan (each launch and readiness report).
    _repo(github / "api")
    assert _ids(root) == ["api", "openswap", "opentag"]


def test_repo_ids_never_collide(root, home, tmp_path):
    cli.add_worker_workspace(root, "openswap", tmp_path / "results-only")
    _repo(home / "a" / "openswap")
    _repo(home / "GitHub" / "openswap")
    _repo(home / "GitHub" / "Notes")
    standalone = cli.add_work_folder(root, home / "a" / "openswap").workspace
    assert standalone.workspace_id == "openswap-2"
    cli.add_work_folder(root, home / "GitHub")
    ids = _ids(root)
    assert ids == ["research", "openswap", "openswap-2", "notes", "openswap-3"]
    assert len(set(ids)) == len(ids)


def test_a_repo_already_added_on_its_own_is_not_listed_twice(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    cli.add_work_folder(root, home / "GitHub")
    assert _ids(root) == ["openswap"]
    assert cli.add_work_folder(root, repo).added is False


def test_a_folder_without_repos_is_refused_unless_direct(root, home):
    notes = home / "Notes"
    notes.mkdir()
    os.chmod(notes, 0o755)
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_work_folder(root, notes)
    assert refused.value.code == "work_not_a_repo" and "--direct" in cli._WORKSPACE_MESSAGES["work_not_a_repo"]
    workspace = cli.add_work_folder(root, notes, mode="direct").workspace
    assert workspace.mode == "direct" and workspace.work_root == notes.resolve()


def test_the_folder_policy_still_applies(root, home):
    _repo(home / "Library" / "repo")
    for folder, code in ((home, "readable_home"), (home / "Library" / "repo", "readable_private"),
                         (root, "readable_exposes_credentials")):
        with pytest.raises(cli.WorkspaceError) as refused:
            cli.add_work_folder(root, folder, mode="direct")
        assert refused.value.code == code


def test_a_read_only_folder_becomes_a_work_folder_under_its_id(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    legacy = cli.add_readable_folder(root, repo).workspace
    upgraded = cli.add_work_folder(root, repo).workspace
    assert upgraded.workspace_id == legacy.workspace_id and upgraded.output_root == legacy.output_root
    (saved,) = load_worker_settings(root).workspaces
    assert saved.work_root == repo.resolve() and saved.readonly_roots == ()


# --- the mode -------------------------------------------------------------------------------


def test_direct_mode_is_set_on_this_mac_only(root, home, capsys):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    assert cli.main(["workspace", "mode", "openswap", "direct", "--json"], backup_root=root) == 0
    assert json.loads(capsys.readouterr().out)["workspace"]["mode"] == "direct"
    assert load_worker_settings(root).workspaces[0].mode == "direct"
    cli.add_work_folder(root, home / "GitHub")
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.set_workspace_mode(root, "missing", "direct")
    assert refused.value.code == "workspace_not_found"
    _repo(home / "GitHub" / "api")
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.set_workspace_mode(root, "api", "direct")
    assert refused.value.code == "mode_on_parent"
    cli.add_readable_folder(root, _repo(home / "Docs"))
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.set_workspace_mode(root, "docs", "direct")
    assert refused.value.code == "mode_not_work"


def test_settings_keep_the_mode_and_older_settings_still_load(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo, mode="direct")
    raw = json.loads((root / "settings.json").read_text())
    config = raw["worker"]["workspaces"]["openswap"]
    assert config["workRoot"] == str(repo.resolve()) and config["mode"] == "direct" and "repos" not in config
    config["mode"] = "anything"
    (root / "settings.json").write_text(json.dumps(raw))
    assert cli.is_builtin_default_registry(root, load_worker_settings(root).workspaces)  # fails closed


# --- launch ---------------------------------------------------------------------------------


def test_each_task_gets_its_own_worktree_and_branch(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    (repo / "README.md").write_text("owner's uncommitted edit\n")
    cli.add_work_folder(root, repo)
    runtime = _runtime(root)
    one = runtime._resolve_workspace("openswap", "a" * 32)
    two = runtime._resolve_workspace("openswap", "b" * 32)
    results = (home / "OpenSwap Research").resolve()
    assert one.work_dir == results / ".worktrees" / "openswap" / ("a" * 32)
    assert one.branch == "openswap/aaaaaaaa" and two.branch == "openswap/bbbbbbbb"
    assert one.output_root == results / "openswap" / ("a" * 32) and one.cwd == one.work_dir
    assert (one.work_dir / "README.md").read_text() == "hello\n"  # from HEAD, not the owner's edit
    assert (repo / "README.md").read_text() == "owner's uncommitted edit\n"  # never touched
    common = (repo / ".git").resolve()
    assert set(one.write_paths) == {one.work_dir, common / "objects", common / "worktrees" / ("a" * 32),
                                    common / "refs" / "heads" / "openswap",
                                    common / "logs" / "refs" / "heads" / "openswap"}
    assert one.read_paths == (common,)
    env = dict(one.env)
    assert env["GIT_CONFIG_KEY_0"] == "gc.auto" and env["GIT_AUTHOR_EMAIL"] == "openswap-task@localhost"
    if os.name == "posix":
        assert (one.work_dir.stat().st_mode & 0o777) == 0o700


def test_direct_mode_launches_in_the_folder(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo, mode="direct")
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert resolved.work_dir == repo.resolve() and resolved.write_paths == (repo.resolve(),)
    assert not (home / "OpenSwap Research" / ".worktrees").exists()


def test_a_repo_without_commits_or_gone_is_refused(root, home):
    empty = _repo(home / "GitHub" / "empty", commit=False)
    cli.add_work_folder(root, empty)
    with pytest.raises(WorkspaceRefused) as refused:
        _runtime(root)._resolve_workspace("empty", "a" * 32)
    assert refused.value.code == "work_not_a_repo"
    moved = _repo(home / "GitHub" / "moved")
    cli.add_work_folder(root, moved)
    shutil.rmtree(moved)
    with pytest.raises(WorkspaceRefused) as refused:
        _runtime(root)._resolve_workspace("moved", "a" * 32)
    assert refused.value.code == "readable_unavailable"


# --- lifecycle --------------------------------------------------------------------------------


def _finish(runtime, *job_ids):
    finished = set(job_ids)
    return lambda job_id: job_id in finished


def test_finished_clean_worktrees_go_and_branches_stay(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    runtime = _runtime(root)
    clean = runtime._resolve_workspace("openswap", "a" * 32)
    dirty = runtime._resolve_workspace("openswap", "b" * 32)
    running = runtime._resolve_workspace("openswap", "c" * 32)
    (clean.work_dir / "new.txt").write_text("x")
    _git(clean.work_dir, "add", "new.txt")
    _git(clean.work_dir, "commit", "-q", "-m", "task work")
    (dirty.work_dir / "scratch.txt").write_text("uncommitted")
    results = home / "OpenSwap Research"
    removed = worktrees.sweep(results, _finish(runtime, "a" * 32, "b" * 32))
    assert [item.job_id for item in removed] == ["a" * 32]
    assert not clean.work_dir.exists() and dirty.work_dir.exists() and running.work_dir.exists()
    assert "openswap/aaaaaaaa" in _git(repo, "branch", "--list", "openswap/*")
    assert _git(repo, "log", "-1", "--format=%s", "openswap/aaaaaaaa") == "task work"
    listed = {item.job_id: item for item in worktrees.list_all(results)}
    assert listed["b" * 32].dirty is True and listed["c" * 32].dirty is False
    worktrees.sweep(results, _finish(runtime, "b" * 32), force=True)
    assert not dirty.work_dir.exists() and "openswap/bbbbbbbb" in _git(repo, "branch", "--list")


def test_a_locked_worktree_or_a_deleted_repo(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    runtime = _runtime(root)
    locked = runtime._resolve_workspace("openswap", "a" * 32)
    _git(repo, "worktree", "lock", str(locked.work_dir))
    results = home / "OpenSwap Research"
    assert worktrees.sweep(results, _finish(runtime, "a" * 32)) == []
    assert worktrees.list_all(results)[0].locked
    other = _repo(home / "GitHub" / "other")
    cli.add_work_folder(root, other)
    orphan = runtime._resolve_workspace("other", "b" * 32)
    shutil.rmtree(other)
    removed = worktrees.sweep(results, _finish(runtime, "b" * 32))
    assert [item.job_id for item in removed] == ["b" * 32] and not orphan.work_dir.exists()


def test_the_next_launch_sweeps_finished_tasks(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    runtime = _runtime(root)
    first = runtime._resolve_workspace("openswap", "a" * 32)
    _runtime(root, running=False)._resolve_workspace("openswap", "b" * 32)  # no record: counts as finished
    assert not first.work_dir.exists()


def test_worktrees_command_lists_and_prunes(root, home, capsys):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert cli.main(["worktrees", "--json"], backup_root=root) == 0
    (item,) = json.loads(capsys.readouterr().out)["worktrees"]
    assert item["branch"] == "openswap/aaaaaaaa" and item["dirty"] is False and item["finished"] is True
    assert cli.main(["worktrees", "prune"], backup_root=root) == 0
    assert "Removed 1 finished task worktree; branches are kept." in capsys.readouterr().out
    assert not resolved.work_dir.exists()


# --- the sandbox's write scope ----------------------------------------------------------------


def test_the_codex_config_grants_exactly_the_git_paths(root, home):
    from openswap.worker.codex_exec import codex_config

    cli.add_work_folder(root, _repo(home / "GitHub" / "openswap"))
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    text = codex_config((), resolved.write_paths, resolved.read_paths)
    for path in resolved.write_paths:
        assert f'"{path}" = "write"' in text
    assert f'"{resolved.read_paths[0]}" = "read"' in text
    assert '":root" = "deny"' in text


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists(),
                    reason="Seatbelt is macOS only")
def test_under_seatbelt_a_commit_works_and_writes_outside_are_denied(root, home, tmp_path):
    from openswap.worker.claude_exec import seatbelt_profile

    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    profile_dir = tmp_path / "profile"
    run_tmp = tmp_path / "run" / "tmp"
    profile_dir.mkdir()
    run_tmp.mkdir(parents=True)
    text = seatbelt_profile(output_root=resolved.work_dir, profile=profile_dir, run_tmp=run_tmp, home=home,
                            backup_root=root, write_paths=resolved.write_paths, read_paths=resolved.read_paths,
                            user_dirs=[])
    sb = tmp_path / "task.sb"
    sb.write_text(text)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(run_tmp), **dict(resolved.env)}

    def sandboxed(script: str) -> int:
        return subprocess.run(["/usr/bin/sandbox-exec", "-f", str(sb), "/bin/sh", "-c", script],
                              cwd=resolved.work_dir, env=env, capture_output=True).returncode

    assert sandboxed("echo change > work.txt && git add work.txt && git commit -q -m 'task work'") == 0
    assert _git(repo, "log", "-1", "--format=%s", resolved.branch) == "task work"
    assert sandboxed(f"echo x > '{repo}/README.md'") != 0  # the owner's copy
    assert (repo / "README.md").read_text() == "hello\n"
    assert sandboxed(f"echo x > '{home}/outside.txt'") != 0
    assert sandboxed("git update-ref refs/heads/main HEAD") != 0  # the owner's branches
    assert sandboxed(f"echo x > '{repo}/.git/config'") != 0
    assert _git(repo, "rev-parse", "main") != _git(repo, "rev-parse", resolved.branch)


# --- setup ------------------------------------------------------------------------------------


def test_the_summary_names_the_mode_only_when_direct(root, home):
    from openswap.worker import guided_setup

    cli.add_work_folder(root, _repo(home / "GitHub" / "a"))
    cli.add_work_folder(root, _repo(home / "GitHub" / "b"), mode="direct")
    assert guided_setup.readiness(root).readable == ("a (a)", "b (b, direct)")


def test_setup_advanced_offers_the_direct_mode(root, home, monkeypatch, capsys):
    from openswap.worker import guided_setup

    monkeypatch.setattr(cli, "_managed_worker_loaded", lambda: False)
    configure_worker_service(root, "http://127.0.0.1:8765", "worker-1")
    _repo(home / "GitHub")
    answers = iter(["n", "", "1", "y"])

    def read(prompt):
        print(prompt)
        return next(answers)

    cli._guided_setup(root, interactive=True, read_line=read, advanced=True)
    out = capsys.readouterr().out
    assert f"{guided_setup.FOLDER_USE}. {guided_setup.FOLDER_COPY}" in out
    assert "Work in ~/GitHub itself, without a copy? [y/N]" in out
    assert "✓ github works in the folder itself." in out
    assert load_worker_settings(root).workspaces[0].mode == "direct"
    assert "  ✓ Folders     github (GitHub, direct)" in out
