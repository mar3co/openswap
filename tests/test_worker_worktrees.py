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
    objects = results / ".worktrees" / "openswap" / f"{'a' * 32}.objects"
    # Never the repo's shared object store (the task writes its own objects), and
    # no ref at all: the worker commits the task's work to its branch.
    assert set(one.write_paths) == {one.work_dir, objects, common / "worktrees" / ("a" * 32)}
    assert one.read_paths == (common,)
    assert dict(one.env)["GIT_OBJECT_DIRECTORY"] == str(objects)
    assert dict(one.env)["GIT_ALTERNATE_OBJECT_DIRECTORIES"] == str(common / "objects")
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
    worktrees.remove_tree(moved)
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
    listed = {item.job_id: item for item in worktrees.list_all(results)}
    assert listed["b" * 32].dirty is True and listed["c" * 32].dirty is False
    # A finished task's leftovers are committed to its branch first (a task stopped
    # by Stop, a timeout or a restart never reached the adapter's own finish).
    removed = worktrees.sweep(results, _finish(runtime, "a" * 32, "b" * 32))
    assert [item.job_id for item in removed] == ["a" * 32, "b" * 32]
    assert not clean.work_dir.exists() and not dirty.work_dir.exists() and running.work_dir.exists()
    assert "openswap/aaaaaaaa" in _git(repo, "branch", "--list", "openswap/*")
    assert _git(repo, "log", "-1", "--format=%s", "openswap/aaaaaaaa") == "task work"
    assert _git(repo, "show", "openswap/bbbbbbbb:scratch.txt") == "uncommitted"


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
    worktrees.remove_tree(other)
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
    from openswap.worker.codex_exec import _toml_string, codex_config

    cli.add_work_folder(root, _repo(home / "GitHub" / "openswap"))
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    text = codex_config((), resolved.write_paths, resolved.read_paths)
    for path in resolved.write_paths:
        assert f'{_toml_string(str(path))} = "write"' in text
    assert f'{_toml_string(str(resolved.read_paths[0]))} = "read"' in text
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

    assert sandboxed("echo change > work.txt && git add work.txt") == 0
    assert sandboxed("git commit -q -m 'task work'") != 0  # no ref is writable, not even its own
    # The commit's objects are in the task's own folder until the worker imports them.
    assert worktrees.finish(resolved.worktree, "left over") is True
    assert _git(repo, "log", "-1", "--format=%s", resolved.branch) == "left over"
    assert _git(repo, "show", f"{resolved.branch}:work.txt") == "change"
    assert sandboxed(f"echo x > '{repo}/.git/objects/planted'") != 0  # the shared object store
    assert sandboxed(f"rm -rf '{repo}/.git/objects/pack'") != 0 or (repo / ".git" / "objects" / "pack").exists()
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
    # The last answer keeps the default per-Mac permission limit (Enter).
    answers = iter(["n", "", "1", "y", ""])

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
    assert guided_setup.PERMISSIONS_OFFER in out and "Permissions" not in out.split(guided_setup.PERMISSIONS_OFFER)[1]


def test_setup_advanced_offers_a_per_mac_permission_limit(root, home, monkeypatch, capsys):
    from openswap.worker import guided_setup

    monkeypatch.setattr(cli, "_managed_worker_loaded", lambda: False)
    configure_worker_service(root, "http://127.0.0.1:8765", "worker-1")
    answers = iter(["n", "", "", "no-shell"])

    def read(prompt):
        print(prompt)
        return next(answers, "")

    cli._guided_setup(root, interactive=True, read_line=read, advanced=True)
    out = capsys.readouterr().out
    assert cli.permission_override(root) == "no-shell"
    assert "✓ This Mac limits every remote task: no shell commands" in out
    # The summary shows the limit because it is not the default.
    assert "Permissions" in out.split(guided_setup.PERMISSIONS_OFFER)[1] and "no-shell (this Mac)" in out
    # Without --advanced nothing is asked, and the default is not shown.
    cli.set_permission_override(root, "follow")
    answers = iter(["n", "", ""])
    cli._guided_setup(root, interactive=True, read_line=read, advanced=False)
    out = capsys.readouterr().out
    assert guided_setup.PERMISSIONS_OFFER not in out and "Permissions" not in out


# --- review fixes: nothing of the repo runs as the worker; stable IDs; own objects ---------------


def test_checkout_and_status_never_run_the_repos_filters(root, home, tmp_path):
    repo = _repo(home / "GitHub" / "openswap")
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.evil.smudge", f"sh -c 'touch {marker}; cat'")
    _git(repo, "config", "filter.evil.clean", f"sh -c 'touch {marker}; cat'")
    _git(repo, "config", "core.fsmonitor", f"sh -c 'touch {marker}'")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n")
    (repo / "data.txt").write_text("data\n")
    _git(repo, "-c", "filter.evil.clean=cat", "-c", "core.fsmonitor=false", "add", ".gitattributes", "data.txt")
    _git(repo, "-c", "filter.evil.clean=cat", "-c", "core.fsmonitor=false", "commit", "-q", "-m", "filtered")
    marker.unlink(missing_ok=True)
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert (resolved.work_dir / "data.txt").read_text() == "data\n"
    (resolved.work_dir / "data.txt").write_text("changed\n")
    assert worktrees.finish(resolved.worktree, "left over") is True
    assert not marker.exists()


def test_finish_imports_objects_and_commits_what_was_left(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    env = {**os.environ, **dict(resolved.env)}
    (resolved.work_dir / "one.txt").write_text("1")
    subprocess.run(["git", "add", "one.txt"], cwd=resolved.work_dir, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "by the task"], cwd=resolved.work_dir, env=env, check=True)
    (resolved.work_dir / "two.txt").write_text("2")  # left uncommitted
    assert worktrees.finish(resolved.worktree, "left over") is True
    assert _git(repo, "log", "--format=%s", resolved.branch).splitlines()[:2] == ["left over", "by the task"]
    assert _git(repo, "show", f"{resolved.branch}:two.txt") == "2"
    assert worktrees.is_dirty(resolved.worktree) is False
    assert worktrees.remove(resolved.work_dir) is True
    assert not resolved.worktree.objects.exists()
    assert _git(repo, "show", f"{resolved.branch}:one.txt") == "1"  # still readable from the repo itself


def test_a_tampered_worktree_is_never_finished_or_run_by_the_worker(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    _git(repo, "branch", "dev")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    (tree.git_dir / "HEAD").write_text("ref: refs/heads/dev\n")  # the task switched branches
    (resolved.work_dir / "x.txt").write_text("x")
    dev = _git(repo, "rev-parse", "dev")
    assert worktrees.intact(tree) is False and worktrees.finish(tree, "left over") is False
    assert _git(repo, "rev-parse", "dev") == dev
    (tree.git_dir / "HEAD").write_text(f"ref: refs/heads/{tree.branch}\n")
    (tree.git_dir / "commondir").write_text("/somewhere/else/.git\n")
    assert worktrees.finish(tree, "left over") is False
    # A rewritten .git file in the worktree is never followed: the worker uses its own record.
    if os.name == "posix":  # Windows keeps the .git file hidden and read-only
        (resolved.work_dir / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    assert worktrees.load_record(resolved.work_dir) == tree
    listed = {item.job_id: item for item in worktrees.list_all(home / "OpenSwap Research")}
    assert listed["a" * 32].intact is False
    assert worktrees.sweep(home / "OpenSwap Research", lambda _job: True) == []  # kept for inspection


def _plant(tree, data: bytes, name: str | None = None) -> str:
    """A loose object file in the task's own object folder, named ``name`` or by its content's SHA-1."""
    import hashlib
    import zlib

    name = name or hashlib.sha1(data).hexdigest()
    folder = tree.objects / name[:2]
    folder.mkdir(exist_ok=True)
    (folder / name[2:]).write_bytes(zlib.compress(data))
    return name


def _in_store(repo, name: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", name], cwd=repo, capture_output=True,
                          env={**os.environ, "GIT_DIR": str(repo / ".git")}).returncode == 0


def test_nothing_the_branch_does_not_need_is_ever_imported(root, home):
    """Bad, malformed, foreign or huge loose objects the task left in its folder
    never reach the repo's store: only what the branch needs is imported."""
    import zlib

    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    head_blob = _git(repo, "rev-parse", "HEAD:README.md")
    _plant(tree, b"blob 4\0evil", head_blob)  # an existing name, wrong content
    wrong = _plant(tree, b"blob 3\0bad", "ab" * 20)  # content that does not hash to its name
    headerless = _plant(tree, b"no git header here")  # hashes to its name, but is no git object
    well_formed = _plant(tree, b"blob 6\0spare!")  # a valid object nothing refers to
    (tree.objects / "cd").mkdir()
    (tree.objects / "cd" / ("e" * 38)).write_bytes(zlib.compress(b"blob 99999999\0" + b"\0" * (8 << 20), 9))
    (resolved.work_dir / "work.txt").write_text("task work")
    assert worktrees.finish(tree, "left over") is True
    assert _git(repo, "show", f"{resolved.branch}:work.txt") == "task work"
    assert _git(repo, "cat-file", "-p", head_blob) == "hello"
    for name in (wrong, headerless, well_formed, "cd" + "e" * 38):
        assert not _in_store(repo, name)
    subprocess.run(["git", "fsck", "--no-dangling"], cwd=repo, check=True, capture_output=True)


def test_a_malformed_object_the_work_needs_keeps_the_branch_where_it_was(root, home):
    """A task can name a malformed object after a file it leaves in the checkout,
    so the commit would need it: git's own checks refuse it, and the branch
    stays put with the worktree kept."""
    import hashlib

    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    content = b"task work\n"
    name = hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()
    _plant(tree, b"blob 3\0bad", name)  # the file's blob name, other bytes
    (resolved.work_dir / "work.txt").write_bytes(content)
    tip = _git(repo, "rev-parse", resolved.branch)
    assert worktrees.finish(tree, "left over") is False
    assert _git(repo, "rev-parse", resolved.branch) == tip
    assert not _in_store(repo, name)
    subprocess.run(["git", "fsck", "--no-dangling"], cwd=repo, check=True, capture_output=True)
    assert worktrees.remove(resolved.work_dir) is False


def test_repo_ids_stay_with_their_repo(root, home):
    github = home / "GitHub"
    _repo(github / "foo.bar")
    cli.add_work_folder(root, github)
    assert _ids(root) == ["foo-bar"]
    _repo(github / "foo bar")  # sorts first and would take foo-bar if IDs were recomputed
    _repo(github / "abc")
    assert dict((w.work_root.name, w.workspace_id) for w in cli.launchable_workspaces(
        root, load_worker_settings(root).workspaces) if w.work_root is not None) == {
        "abc": "abc", "foo bar": "foo-bar-2", "foo.bar": "foo-bar"}
    # Gone and back: the same ID; and an approved folder never takes a repo's ID.
    worktrees.remove_tree(github / "foo.bar")
    assert "foo-bar" not in _ids(root)
    _repo(github / "foo.bar")
    assert "foo-bar" in _ids(root)
    other = _repo(home / "Code" / "foo-bar")
    assert cli.add_work_folder(root, other).workspace.workspace_id == "foo-bar-3"
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_worker_workspace(root, "abc", home / "results-abc")
    assert refused.value.code == "workspace_exists"


def test_results_are_found_after_the_repo_is_gone(root, home):
    github = home / "GitHub"
    repo = _repo(github / "openswap")
    cli.add_work_folder(root, github)
    assert _ids(root) == ["openswap"]
    worktrees.remove_tree(repo)
    workspaces = load_worker_settings(root).workspaces
    assert cli.results_folder(root, "openswap", workspaces) == home / "OpenSwap Research" / "openswap"
    assert cli.results_folder(root, "missing", workspaces) is None
    assert cli.results_folder(root, "../x", workspaces) is None


def test_codex_shells_get_the_git_settings_and_a_read_only_sandbox_writes_nothing(root, home):
    from openswap.worker.codex_exec import _toml_string, codex_config
    from openswap.worker.permissions import CodexPermissions

    cli.add_work_folder(root, _repo(home / "GitHub" / "openswap"))
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    text = codex_config((), resolved.write_paths, resolved.read_paths, dict(resolved.env))
    assert f"GIT_OBJECT_DIRECTORY = {_toml_string(str(resolved.worktree.objects))}" in text
    assert "gc.auto" in text
    for path in resolved.write_paths:
        assert f'{_toml_string(str(path))} = "write"' in text
    # The account's read-only sandbox (or the per-Mac read-only limit): the
    # worktree and what git needs beside it become read-only too.
    read_only = codex_config((), resolved.write_paths, resolved.read_paths, dict(resolved.env),
                             CodexPermissions(sandbox="read-only").launch("follow"))
    assert '"write"' not in read_only
    for path in resolved.write_paths:
        assert f'{_toml_string(str(path))} = "read"' in read_only


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_a_results_folder_that_turned_unsafe_is_refused_and_not_advertised(root, home):
    from openswap.worker.remote import offerable

    repo = _repo(home / "GitHub" / "openswap")
    workspace = cli.add_work_folder(root, repo).workspace
    workspaces = cli.launchable_workspaces(root, load_worker_settings(root).workspaces)
    assert cli.workspace_refusal(root, workspace, workspaces) is None
    os.chmod(workspace.output_root, 0o755)  # others can now reach the results folder
    assert cli.workspace_refusal(root, workspace, workspaces) == "folder_permissions"
    assert not offerable(root, workspace, workspaces)
    assert cli.refused_workspaces(root) == [("openswap", "folder_permissions")]
    with pytest.raises(WorkspaceRefused) as refused:
        _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert refused.value.code == "folder_permissions"
    assert not (workspace.output_root / ("a" * 32)).exists()
    os.chmod(workspace.output_root, 0o700)
    worktrees.remove_tree(workspace.output_root)
    workspace.output_root.write_text("not a folder")  # replaced by a file
    assert cli.workspace_refusal(root, workspace, workspaces) == "folder_unsafe"
    workspace.output_root.unlink()
    assert cli.workspace_refusal(root, workspace, workspaces) is None  # missing: made at launch
    base = home / "OpenSwap Research" / ".worktrees"
    base.mkdir(mode=0o755, exist_ok=True)
    os.chmod(base, 0o755)
    assert cli.workspace_refusal(root, workspace, workspaces) == "folder_permissions"


def test_filter_driver_names_with_dots_are_disabled_too(root, home, tmp_path):
    repo = _repo(home / "GitHub" / "openswap")
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.evil.dot.smudge", f"sh -c 'touch {marker}; cat'")
    (repo / ".gitattributes").write_text("*.txt filter=evil.dot\n")
    (repo / "data.txt").write_text("data\n")
    _git(repo, "-c", "filter.evil.dot.smudge=cat", "add", ".gitattributes", "data.txt")
    _git(repo, "-c", "filter.evil.dot.smudge=cat", "commit", "-q", "-m", "filtered")
    cli.add_work_folder(root, repo)
    _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert not marker.exists()
    # A driver added later is read again, never served from a stale list.
    _git(repo, "config", "filter.late.smudge", f"sh -c 'touch {marker}; cat'")
    (repo / ".gitattributes").write_text("*.txt filter=late\n")
    _git(repo, "-c", "filter.late.smudge=cat", "commit", "-q", "-am", "late")
    _runtime(root)._resolve_workspace("openswap", "b" * 32)
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="the worker runs on macOS; repack with alternates differs on Windows")
def test_packs_the_task_made_are_imported(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    env = {**os.environ, **dict(resolved.env)}
    (resolved.work_dir / "packed.txt").write_text("packed")
    subprocess.run(["git", "add", "packed.txt"], cwd=resolved.work_dir, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "packed"], cwd=resolved.work_dir, env=env, check=True)
    subprocess.run(["git", "repack", "-adq"], cwd=resolved.work_dir, env=env, check=True)
    assert list((resolved.worktree.objects / "pack").glob("*.pack"))
    assert worktrees.finish(resolved.worktree, "left over") is True
    assert worktrees.remove(resolved.work_dir) is True
    assert _git(repo, "show", f"{resolved.branch}:packed.txt") == "packed"


def test_work_folders_never_overlap_read_only_ones(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_readable_folder(root, home / "GitHub")
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_work_folder(root, repo)
    assert refused.value.code == "work_overlaps_readable"


def test_a_driver_name_with_an_equals_sign_is_disabled_too(root, home, tmp_path):
    repo = _repo(home / "GitHub" / "openswap")
    marker = tmp_path / "filter-ran"
    _git(repo, "config", "filter.evil=x.smudge", f"sh -c 'touch {marker}; cat'")
    (repo / ".gitattributes").write_text("*.txt filter=evil=x\n")
    (repo / "data.txt").write_text("data\n")
    _git(repo, "-c", "core.attributesFile=/dev/null", "add", ".gitattributes", "data.txt")
    _git(repo, "commit", "-q", "-m", "filtered")
    marker.unlink(missing_ok=True)
    cli.add_work_folder(root, repo)
    _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert not marker.exists()


def test_a_failed_setup_leaves_nothing_behind(root, home, monkeypatch):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    real_open = os.open

    def failing_open(path, *args, **kwargs):
        if str(path).endswith(".json") and ".worktrees" in str(path):
            raise OSError("disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(worktrees.os, "open", failing_open)
    with pytest.raises(WorkspaceRefused):
        _runtime(root)._resolve_workspace("openswap", "a" * 32)
    monkeypatch.setattr(worktrees.os, "open", real_open)
    folder = home / "OpenSwap Research" / ".worktrees" / "openswap"
    assert not (folder / ("a" * 32)).exists() and not (folder / f"{'a' * 32}.objects").exists()
    assert "openswap/aaaaaaaa" not in _git(repo, "branch", "--list")
    assert _git(repo, "worktree", "list").count("\n") == 0
    # The same task can be set up again.
    assert _runtime(root)._resolve_workspace("openswap", "a" * 32).work_dir.exists()



@pytest.mark.skipif(os.name == "nt", reason="':' is not allowed in Windows names")
def test_a_repo_path_with_a_colon_still_works(root, home):
    repo = _repo(home / "GitHub" / "a:b")
    cli.add_work_folder(root, repo)
    workspace_id = _ids(root)[0]
    resolved = _runtime(root)._resolve_workspace(workspace_id, "a" * 32)
    env = {**os.environ, **dict(resolved.env)}
    assert dict(resolved.env)["GIT_ALTERNATE_OBJECT_DIRECTORIES"].startswith('"')
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=resolved.work_dir, env=env).returncode == 0
    (resolved.work_dir / "x.txt").write_text("x")
    assert worktrees.finish(resolved.worktree, "left over") is True
    assert _git(repo, "show", f"{resolved.branch}:x.txt") == "x"


def test_a_task_survives_its_linked_working_copy_being_removed(root, home):
    main = _repo(home / "Code" / "main")
    linked = home / "GitHub" / "linked"
    linked.parent.mkdir(parents=True, exist_ok=True)
    _git(main, "worktree", "add", "-q", "-b", "linked", str(linked))
    os.chmod(linked, 0o755)
    cli.add_work_folder(root, linked)
    runtime = _runtime(root)
    resolved = runtime._resolve_workspace("linked", "a" * 32)
    (resolved.work_dir / "edit.txt").write_text("keep me")
    worktrees.remove_tree(linked)  # the approved copy is gone, the shared repo is not
    removed = worktrees.sweep(home / "OpenSwap Research", lambda _job: True)
    assert [item.job_id for item in removed] == ["a" * 32]
    assert _git(main, "show", f"{resolved.branch}:edit.txt") == "keep me"



def test_a_branch_named_openswap_never_blocks_a_launch(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    _git(repo, "branch", "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert resolved.branch == "openswap-aaaaaaaa"
    _git(repo, "branch", "openswap-bbbbbbbb")
    assert _runtime(root)._resolve_workspace("openswap", "b" * 32).branch == "openswap-" + "b" * 32


def test_repos_added_later_never_push_an_offered_one_out(root, home, monkeypatch):
    monkeypatch.setattr(worktrees, "_MAX_CHILD_REPOS", 2)
    github = home / "GitHub"
    _repo(github / "zz-first")
    cli.add_work_folder(root, github)
    assert _ids(root) == ["zz-first"]
    for name in ("aa", "bb", "cc"):
        _repo(github / name)
    ids = _ids(root)
    assert "zz-first" in ids and len(ids) == 3  # the two new ones the cap allows, and the kept one



def test_an_offered_repo_past_the_scan_limit_is_still_listed(root, home, monkeypatch):
    github = home / "GitHub"
    _repo(github / "zz-last")
    cli.add_work_folder(root, github)
    assert _ids(root) == ["zz-last"]
    monkeypatch.setattr(worktrees, "_MAX_SCANNED_CHILDREN", 2)
    for name in ("aa", "bb", "cc"):
        (github / name).mkdir()  # plain folders sorting first fill the scan limit
    assert "zz-last" in _ids(root)


def test_a_folder_of_repos_that_lost_its_repos_is_reported_refused(root, home):
    github = home / "GitHub"
    repo = _repo(github / "openswap")
    cli.add_work_folder(root, github)
    assert cli.refused_workspaces(root) == []
    worktrees.remove_tree(repo)
    assert _ids(root) == []  # nothing left to offer...
    assert cli.refused_workspaces(root) == [("github", "work_no_repos")]  # ...and it says why
    worktrees.remove_tree(github)
    assert cli.refused_workspaces(root) == [("github", "readable_unavailable")]



def test_a_direct_folder_of_repos_that_lost_its_repos_is_reported_refused(root, home):
    github = home / "GitHub"
    repo = _repo(github / "openswap")
    cli.add_work_folder(root, github)
    cli.set_workspace_mode(root, "github", "direct")
    assert cli.refused_workspaces(root) == []
    worktrees.remove_tree(repo)
    assert cli.refused_workspaces(root) == [("github", "work_no_repos")]


def test_a_blocked_branch_name_leaves_nothing_behind(root, home, monkeypatch):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)

    def blocked(_repo, _job_id):
        raise worktrees.WorktreeError("branch_unavailable")

    real_free_branch = worktrees._free_branch
    monkeypatch.setattr(worktrees, "_free_branch", blocked)
    with pytest.raises(WorkspaceRefused):
        _runtime(root)._resolve_workspace("openswap", "a" * 32)
    folder = home / "OpenSwap Research" / ".worktrees" / "openswap"
    assert not (folder / f"{'a' * 32}.objects").exists()
    monkeypatch.setattr(worktrees, "_free_branch", real_free_branch)
    assert _runtime(root)._resolve_workspace("openswap", "a" * 32).work_dir.exists()  # a retry works



def test_a_tag_sharing_a_branch_name_never_hides_the_branch(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    _git(repo, "branch", "openswap")
    _git(repo, "tag", "openswap")
    cli.add_work_folder(root, repo)
    assert _runtime(root)._resolve_workspace("openswap", "a" * 32).branch == "openswap-aaaaaaaa"


def test_a_failed_checkout_removes_the_branch_it_made(root, home, monkeypatch):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    real_git = worktrees.git

    def failing_checkout(args, cwd, **kwargs):
        result = real_git(args, cwd, **kwargs)
        if args[:2] == ["worktree", "add"]:
            raise worktrees.WorktreeError("git_failed")  # the branch exists, the checkout "failed"
        return result

    monkeypatch.setattr(worktrees, "git", failing_checkout)
    with pytest.raises(WorkspaceRefused):
        _runtime(root)._resolve_workspace("openswap", "a" * 32)
    monkeypatch.setattr(worktrees, "git", real_git)
    assert "openswap/aaaaaaaa" not in _git(repo, "branch", "--list")
    folder = home / "OpenSwap Research" / ".worktrees" / "openswap"
    assert not (folder / ("a" * 32)).exists() and not (folder / f"{'a' * 32}.objects").exists()
    assert _runtime(root)._resolve_workspace("openswap", "a" * 32).branch == "openswap/aaaaaaaa"



def test_direct_mode_on_a_folder_of_repos_applies_to_each_repo(root, home):
    github = home / "GitHub"
    _repo(github / "openswap")
    _repo(github / "opentag")
    parent = cli.add_work_folder(root, github, mode="direct").workspace
    assert parent.repos is True and parent.mode == "direct"  # still a folder of repos
    launchable = cli.launchable_workspaces(root, load_worker_settings(root).workspaces)
    assert [(w.workspace_id, w.mode, w.work_root.name) for w in launchable] == [
        ("openswap", "direct", "openswap"), ("opentag", "direct", "opentag")]
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    assert resolved.work_dir == (github / "openswap").resolve()  # the repo, never the parent
    assert resolved.write_paths == ((github / "openswap").resolve(),)
    # And back: the same parent, each repo in its own worktree again.
    assert cli.set_workspace_mode(root, "github", "worktree").repos is True
    assert _runtime(root)._resolve_workspace("opentag", "b" * 32).branch == "openswap/bbbbbbbb"



@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_links_planted_in_the_admin_folder_are_never_written_through(root, home, tmp_path):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    victims = {}
    for name in ("COMMIT_EDITMSG", "ORIG_HEAD", "index.lock", "logs/HEAD", "FETCH_HEAD"):
        victim = tmp_path / f"victim-{name.replace('/', '-')}"
        victim.write_text("owner's file")
        target = tree.git_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(victim)
        victims[name] = victim
    (resolved.work_dir / "work.txt").write_text("task work")
    assert worktrees.is_dirty(tree) is True
    assert worktrees.finish(tree, "left over") is True
    assert _git(repo, "show", f"{resolved.branch}:work.txt") == "task work"
    for victim in victims.values():
        assert victim.read_text() == "owner's file"
    assert worktrees.remove(resolved.work_dir) is True
    for victim in victims.values():
        assert victim.read_text() == "owner's file"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_linked_admin_head_is_not_intact(root, home, tmp_path):
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    tree = _runtime(root)._resolve_workspace("openswap", "a" * 32).worktree
    real = tmp_path / "HEAD"
    real.write_text((tree.git_dir / "HEAD").read_text())
    (tree.git_dir / "HEAD").unlink()
    (tree.git_dir / "HEAD").symlink_to(real)
    assert worktrees.intact(tree) is False



def test_ignored_files_the_task_left_keep_the_worktree(root, home):
    repo = _repo(home / "GitHub" / "openswap")
    (repo / ".gitignore").write_text("build/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore build")
    cli.add_work_folder(root, repo)
    runtime = _runtime(root)
    resolved = runtime._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    assert worktrees.is_dirty(tree) is False
    (resolved.work_dir / "notes.txt").write_text("tracked work")
    (resolved.work_dir / "build").mkdir()
    (resolved.work_dir / "build" / "out.bin").write_text("ignored output")
    # The rest is committed; the ignored output is not, so the worktree stays.
    assert worktrees.finish(tree, "left over") is False
    assert _git(repo, "show", f"{resolved.branch}:notes.txt") == "tracked work"
    assert worktrees.is_dirty(tree) is True
    assert worktrees.remove(resolved.work_dir) is False
    results = home / "OpenSwap Research"
    assert worktrees.sweep(results, _finish(runtime, "a" * 32)) == []
    assert (resolved.work_dir / "build" / "out.bin").read_text() == "ignored output"
    # Once the owner has looked (or with --force), it goes.
    (resolved.work_dir / "build" / "out.bin").unlink()
    (resolved.work_dir / "build").rmdir()
    assert worktrees.finish(tree, "left over") is True
    assert worktrees.remove(resolved.work_dir) is True



def test_adding_a_folder_of_repos_again_keeps_the_one_saved(root, home):
    github = home / "GitHub"
    _repo(github / "openswap")
    _repo(github / "opentag")
    first = cli.add_work_folder(root, github)
    again = cli.add_work_folder(root, github)
    assert first.added is True and again.added is False
    assert again.workspace.workspace_id == first.workspace.workspace_id
    assert again.repos == first.repos == ("openswap", "opentag")
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == [first.workspace.workspace_id]
    # A repo inside it is found too, as the repo's own folder.
    child = cli.add_work_folder(root, github / "openswap")
    assert child.added is False and child.workspace.workspace_id == "openswap"
    assert len(load_worker_settings(root).workspaces) == 1



def test_a_queued_repo_task_holds_its_folder_of_repos(root, home):
    """A task sent to a repo in a folder of repos uses the repo's ID, never the
    parent's: the parent's mode and approval stay while it may still run."""
    from openswap.worker.models import JobState
    from openswap.worker.journal import LocalJobStore

    github = home / "GitHub"
    _repo(github / "openswap")
    _repo(github / "opentag")
    parent = cli.add_work_folder(root, github).workspace
    other = _repo(home / "elsewhere" / "tool")
    cli.add_work_folder(root, other)
    runtime = _runtime(root)
    store = LocalJobStore(root)
    epoch = store.current_epoch()
    job = store.create(_submission(workspace_id="opentag"), owner_ref="local-user", worker_epoch=epoch)
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.set_workspace_mode(root, parent.workspace_id, "direct")
    assert refused.value.code == "workspace_in_use"
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.remove_worker_workspace(root, parent.workspace_id)
    assert refused.value.code == "workspace_in_use"
    assert runtime._resolve_workspace("opentag", job.job_id).branch is not None  # still a worktree
    store.transition(job.job_id, expected_states=(JobState.QUEUED,), new_state=JobState.CANCELLED,
                     worker_epoch=epoch, expected_generation=job.generation)
    assert cli.set_workspace_mode(root, parent.workspace_id, "direct").mode == "direct"
    cli.remove_worker_workspace(root, parent.workspace_id)
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["tool"]



def _sparse_repo(path: Path) -> Path:
    repo = _repo(path)
    for name in ("a", "b"):
        (repo / name).mkdir()
        (repo / name / name).write_text(name)
    _git(repo, "add", "a", "b")
    _git(repo, "commit", "-q", "-m", "two folders")
    _git(repo, "sparse-checkout", "set", "a")
    assert not (repo / "b").exists()
    return repo


def test_a_sparse_owner_checkout_gives_the_task_a_full_copy(root, home):
    repo = _sparse_repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    assert (resolved.work_dir / "a" / "a").exists() and (resolved.work_dir / "b" / "b").exists()
    assert not (tree.git_dir / "config.worktree").exists()
    assert not (tree.git_dir / "info" / "sparse-checkout").exists()
    assert worktrees.is_dirty(tree) is False
    (resolved.work_dir / "b" / "b").write_text("task work")
    # The owner's rules (cone "a") never hide the task's change or delete "b".
    assert worktrees.finish(tree, "left over") is True
    assert _git(repo, "show", f"{resolved.branch}:b/b") == "task work"
    assert _git(repo, "show", f"{resolved.branch}:a/a") == "a"
    assert not (repo / "b").exists()  # the owner's checkout stays sparse


def test_a_task_that_makes_its_copy_sparse_keeps_its_worktree(root, home):
    repo = _sparse_repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    (resolved.work_dir / "b" / "b").write_text("task work")
    _git(resolved.work_dir, "sparse-checkout", "set", "b")
    assert not (resolved.work_dir / "a").exists()
    tip = _git(repo, "rev-parse", resolved.branch)
    assert worktrees.finish(tree, "left over") is False
    assert _git(repo, "rev-parse", resolved.branch) == tip  # nothing committed, "a" never deleted
    assert worktrees.is_dirty(tree) is None
    assert worktrees.remove(resolved.work_dir) is False
    assert (resolved.work_dir / "b" / "b").read_text() == "task work"



def test_a_tag_named_like_the_branch_never_vouches_for_its_objects(root, home):
    """The final check walks ``refs/heads/<branch>``: a tag of the same name
    (pointing at objects the repo has) must not pass for a branch whose
    commit still needs the task's own object folder."""
    repo = _repo(home / "GitHub" / "openswap")
    cli.add_work_folder(root, repo)
    resolved = _runtime(root)._resolve_workspace("openswap", "a" * 32)
    tree = resolved.worktree
    _git(repo, "tag", resolved.branch, "HEAD")
    (resolved.work_dir / "work.txt").write_text("task work")
    # The task's own git stores the blob in its object folder only.
    subprocess.run(["git", "add", "work.txt"], cwd=resolved.work_dir, check=True, capture_output=True,
                   env={**os.environ, **worktrees.task_env(repo, tree)})
    real_import = worktrees.import_objects
    worktrees.import_objects = lambda tree, commit=None: True  # and it never reaches the repo's store
    try:
        assert worktrees.finish(tree, "left over") is False
    finally:
        worktrees.import_objects = real_import
    assert not (tree.path.parent / f"{tree.path.name}.finished").exists()
