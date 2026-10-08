"""Per-task git worktrees for work folders.

A work folder is where a remote Claude or Codex session is launched, like
running ``claude`` in a repo. By default each task works in its own git
worktree of the chosen repo, on a new branch ``openswap/<short task id>`` cut
from the repo's current ``HEAD``, so it can edit and commit freely without
touching the owner's working copy or another task. The worktree lives under
``~/OpenSwap Research/.worktrees/<folder id>/<task id>`` (owner-only).

When a task ends its branch is always kept. Its worktree directory is
removed if it is clean, and kept for inspection if it holds uncommitted
work; ``openswap worker worktrees`` lists them and ``prune`` removes them.

Every git command here runs as the worker, outside the task's sandbox, with
the repo's hooks disabled (``core.hooksPath=/dev/null``) so creating or
removing a worktree never runs code from the repo.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from openswap import pathid

WORKTREES_DIR = ".worktrees"
BRANCH_PREFIX = "openswap"
_GIT_TIMEOUT_S = 300
_MAX_CHILD_REPOS = 64
# The task's own git must never start background maintenance that rewrites
# packs or refs outside the paths it may write.
GIT_TASK_CONFIG = (("gc.auto", "0"), ("maintenance.auto", "false"))


class WorktreeError(RuntimeError):
    """A worktree could not be made or removed; ``code`` is a stable, path-free reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _git_env() -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "HOME": str(Path.home()),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if os.name == "nt":  # tests only: git on Windows needs its own environment
        env = {**os.environ, **{k: v for k, v in env.items() if k != "PATH"}}
    return env


def git(args: list[str], cwd: Path, *, timeout: float = _GIT_TIMEOUT_S, check: bool = True) -> str:
    """Run ``git`` with hooks disabled; stdout, stripped. Raises ``WorktreeError("git_failed")``."""
    command = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "submodule.recurse=false", *args]
    try:
        result = subprocess.run(command, cwd=str(cwd), env=_git_env(), capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise WorktreeError("git_unavailable") from None
    if check and result.returncode != 0:
        raise WorktreeError("git_failed")
    return result.stdout.strip()


def is_repo(folder: Path) -> bool:
    """Whether ``folder`` is the top of a git working copy (not just inside one)."""
    folder = Path(folder)
    if not os.path.lexists(folder / ".git"):
        return False
    try:
        top = git(["rev-parse", "--show-toplevel"], folder, timeout=30)
    except WorktreeError:
        return False
    return bool(top) and pathid.same(top, folder)


def child_repos(parent: Path) -> list[Path]:
    """The git repos directly inside ``parent`` (not hidden, not symlinks), by name."""
    out = []
    try:
        children = sorted(Path(parent).iterdir(), key=lambda p: (p.name.lower(), p.name))
    except OSError:
        return []
    for child in children:
        try:
            if child.name.startswith(".") or child.is_symlink() or not child.is_dir():
                continue
        except OSError:
            continue
        if os.path.lexists(child / ".git"):
            out.append(pathid.canonical(child))
            if len(out) >= _MAX_CHILD_REPOS:
                break
    return out


def common_dir(repo: Path) -> Path:
    """The repo's shared ``.git`` directory (objects, refs, config)."""
    text = git(["rev-parse", "--git-common-dir"], repo, timeout=30)
    path = Path(text)
    return pathid.canonical(path if path.is_absolute() else Path(repo) / path)


def worktrees_root(results_root: Path) -> Path:
    return Path(results_root) / WORKTREES_DIR


def task_path(results_root: Path, workspace_id: str, job_id: str) -> Path:
    return worktrees_root(results_root) / workspace_id / job_id


@dataclass(frozen=True)
class Worktree:
    """A task's worktree and exactly what its sandbox may write for git to work."""

    path: Path
    repo: Path
    common_dir: Path
    git_dir: Path  # common_dir/worktrees/<name>: this worktree's HEAD, index and logs
    branch: str

    @property
    def write_paths(self) -> tuple[Path, ...]:
        """Write grants: the worktree, the object store, its own admin folder and the
        ``openswap/`` branch namespace (refs and their logs; git writes a sibling
        ``.lock`` file to update a ref, so the namespace folder, not the ref file)."""
        namespace = Path("refs") / "heads" / BRANCH_PREFIX
        return (self.path, self.common_dir / "objects", self.git_dir,
                self.common_dir / namespace, self.common_dir / "logs" / namespace)

    @property
    def read_paths(self) -> tuple[Path, ...]:
        """Read grants beyond the worktree: the repo's shared ``.git`` only, never its working copy."""
        return (self.common_dir,)


def remove_tree(path: Path) -> None:
    """``shutil.rmtree`` that also removes read-only files (git's objects on Windows)."""
    import shutil
    import stat

    def retry(function, target, _info):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(target)
        except OSError:
            pass

    import sys

    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


def _private_dirs(path: Path, stop: Path) -> None:
    """Create ``path`` and its parents below ``stop`` owner-only (0700)."""
    missing = []
    current = Path(path)
    while current != stop and not current.exists():
        missing.append(current)
        current = current.parent
    for folder in reversed(missing):
        folder.mkdir(mode=0o700)


def create(repo: Path, results_root: Path, workspace_id: str, job_id: str) -> Worktree:
    """A new worktree of ``repo`` for ``job_id`` on branch ``openswap/<short id>`` from ``HEAD``.

    ``HEAD`` is the commit the owner has checked out, which works without a
    remote or a configured default branch; uncommitted changes in the
    owner's copy are not carried over (and are never touched).
    """
    repo = pathid.canonical(repo)
    if not is_repo(repo):
        raise WorktreeError("not_a_repo")
    try:
        git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], repo, timeout=30)
    except WorktreeError:
        raise WorktreeError("no_commits") from None
    results_root = Path(results_root)
    dest = task_path(results_root, workspace_id, job_id)
    if os.path.lexists(dest):
        raise WorktreeError("worktree_exists")
    try:
        results_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_dirs(dest.parent, results_root)
    except OSError:
        raise WorktreeError("worktree_unavailable") from None
    branch = f"{BRANCH_PREFIX}/{job_id[:8]}"
    if git(["branch", "--list", branch], repo, timeout=30):
        branch = f"{BRANCH_PREFIX}/{job_id}"
    git(["worktree", "add", "--quiet", "-b", branch, str(dest), "HEAD"], repo)
    try:
        # The branch namespace's log folder exists even where reflogs are off,
        # so the sandbox grant names a real folder.
        common = common_dir(dest)
        (common / "logs" / "refs" / "heads" / BRANCH_PREFIX).mkdir(parents=True, exist_ok=True)
        git_dir_text = git(["rev-parse", "--git-dir"], dest, timeout=30)
        git_dir = Path(git_dir_text)
        git_dir = pathid.canonical(git_dir if git_dir.is_absolute() else dest / git_dir)
        os.chmod(dest, 0o700)
    except (WorktreeError, OSError):
        remove(dest, force=True)
        raise WorktreeError("worktree_unavailable") from None
    return Worktree(pathid.canonical(dest), repo, common, git_dir, branch)


def identity(repo: Path) -> dict[str, str]:
    """The repo's commit identity as git environment variables (empty when unset).

    The task's sandbox may not read ``~/.gitconfig``, so the worker reads it
    here and hands it over; without one git would refuse to commit.
    """
    out = {}
    for key, variables in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                           ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        try:
            value = git(["config", "--get", key], repo, timeout=30, check=False)
        except WorktreeError:
            value = ""
        if value and "\n" not in value and len(value) <= 200:
            for variable in variables:
                out[variable] = value
    return out


def task_env(repo: Path) -> dict[str, str]:
    """Environment for the task's own git: no background maintenance, the repo's identity."""
    env = {"GIT_CONFIG_COUNT": str(len(GIT_TASK_CONFIG))}
    for index, (key, value) in enumerate(GIT_TASK_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env.update(identity(repo))
    env.setdefault("GIT_AUTHOR_NAME", "OpenSwap task")
    env.setdefault("GIT_COMMITTER_NAME", "OpenSwap task")
    env.setdefault("GIT_AUTHOR_EMAIL", "openswap-task@localhost")
    env.setdefault("GIT_COMMITTER_EMAIL", "openswap-task@localhost")
    return env


def _repo_of(path: Path) -> Path | None:
    """The main working copy a worktree belongs to, from its ``.git`` file."""
    try:
        text = (Path(path) / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir: "):
        return None
    admin = Path(text[len("gitdir: "):])
    # <repo>/.git/worktrees/<name> -> <repo>
    if admin.parent.name != "worktrees":
        return None
    return admin.parent.parent.parent


def is_dirty(path: Path) -> bool | None:
    """Whether a worktree holds uncommitted or untracked work (None when git cannot tell)."""
    try:
        return bool(git(["status", "--porcelain", "--untracked-files=normal"], path, timeout=60))
    except WorktreeError:
        return None


def remove(path: Path, *, force: bool = False) -> bool:
    """Remove a worktree directory and its admin entry; its branch is kept. Whether it is gone."""
    path = Path(path)
    repo = _repo_of(path)
    if repo is not None and repo.exists():
        # Twice --force also removes a locked worktree.
        args = ["worktree", "remove", *(["--force", "--force"] if force else []), str(path)]
        try:
            git(args, repo)
        except WorktreeError:
            if not force:
                return False
    if force and path.exists():
        remove_tree(path)
    if repo is not None and repo.exists():
        try:
            git(["worktree", "prune"], repo, timeout=60, check=False)
        except WorktreeError:
            pass
    return not path.exists()


@dataclass(frozen=True)
class TaskWorktree:
    """One task's worktree as ``openswap worker worktrees`` lists it."""

    workspace_id: str
    job_id: str
    path: Path
    repo: Path | None
    branch: str | None
    dirty: bool | None
    locked: bool


def _branch(path: Path) -> str | None:
    try:
        name = git(["symbolic-ref", "--quiet", "--short", "HEAD"], path, timeout=30, check=False)
    except WorktreeError:
        return None
    return name or None


def _locked(path: Path) -> bool:
    repo = _repo_of(path)
    try:
        text = (Path(path) / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    admin = Path(text[len("gitdir: "):]) if text.startswith("gitdir: ") else None
    return repo is not None and admin is not None and os.path.lexists(admin / "locked")


def list_all(results_root: Path) -> list[TaskWorktree]:
    root = worktrees_root(results_root)
    out = []
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    for folder in folders:
        try:
            tasks = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        for task in tasks:
            repo = _repo_of(task)
            present = repo is not None and repo.exists()
            out.append(TaskWorktree(
                folder.name, task.name, task, repo,
                _branch(task) if present else None,
                is_dirty(task) if present else None,
                _locked(task),
            ))
    return out


def sweep(results_root: Path, finished, *, force: bool = False) -> list[TaskWorktree]:
    """Remove the worktrees of finished tasks: clean ones, or every one with ``force``.

    ``finished(job_id)`` says whether a task has ended. A dirty, locked or
    unreadable worktree is kept unless ``force``; a worktree whose repo was
    moved or deleted is removed (there is nothing left to inspect it with).
    Returns the ones removed. Branches are always kept.
    """
    removed = []
    for item in list_all(results_root):
        if not finished(item.job_id):
            continue
        repo_gone = item.repo is None or not item.repo.exists()
        if not force and not repo_gone and (item.dirty is not False or item.locked):
            continue
        if remove(item.path, force=force or repo_gone):
            removed.append(item)
            try:
                item.path.parent.rmdir()  # the folder's directory, once it is empty
            except OSError:
                pass
    return removed
