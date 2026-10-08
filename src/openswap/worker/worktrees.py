"""Per-task git worktrees for work folders.

A work folder is where a remote Claude or Codex session is launched, like
running ``claude`` in a repo. By default each task works in its own git
worktree of the chosen repo, on a new branch ``openswap/<short task id>`` cut
from the repo's current ``HEAD``, so it can edit and commit freely without
touching the owner's working copy or another task. The worktree lives under
``~/OpenSwap Research/.worktrees/<folder id>/<task id>`` (owner-only).

The task writes new git objects to its own object folder
(``<task id>.objects``, with the repo's object store as a read-only
alternate), so it can never delete or rewrite the objects the owner's
checkout and other tasks use. When the task ends the worker imports its
objects (each one verified against its name), commits anything left
uncommitted to the task's branch, and keeps the branch. A clean worktree is
then removed; one the worker could not finish is kept for inspection.
``openswap worker worktrees`` lists them and ``prune`` removes them.

Every git command here runs as the worker, outside the task's sandbox. So
none may run code from the repo or from anything the task could write:
hooks are disabled (``core.hooksPath``), as are checkout and clean filters,
``core.fsmonitor``, commit signing and submodule recursion; and git is
pointed at the worktree with explicit ``GIT_DIR``/``GIT_WORK_TREE`` from a
record the task cannot write, after checking the task did not repoint its
admin folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

from openswap import pathid

WORKTREES_DIR = ".worktrees"
BRANCH_PREFIX = "openswap"
_GIT_TIMEOUT_S = 300
_MAX_CHILD_REPOS = 64
_MAX_OBJECT_BYTES = 512 * 1024 * 1024
# The task's own git must never start background maintenance that rewrites
# packs or refs outside the paths it may write.
GIT_TASK_CONFIG = (("gc.auto", "0"), ("maintenance.auto", "false"))
# Settings under which git runs nothing from the repo or from its config.
_SAFE_CONFIG = (
    f"core.hooksPath={os.devnull}", "core.fsmonitor=false", "submodule.recurse=false",
    "commit.gpgSign=false", "tag.gpgSign=false", "core.sshCommand=false", "protocol.allow=never",
)


class WorktreeError(RuntimeError):
    """A worktree could not be made or removed; ``code`` is a stable, path-free reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "HOME": str(Path.home()),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if os.name == "nt":  # tests only: git on Windows needs its own environment
        env = {**os.environ, **{k: v for k, v in env.items() if k != "PATH"}}
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            env.pop(key, None)
    return {**env, **(extra or {})}


def _run(command: list[str], cwd: Path, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, cwd=str(cwd), env=env, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise WorktreeError("git_unavailable") from None


_FILTERS: dict[str, tuple[str, ...]] = {}


def _filter_overrides(cwd: Path, env: dict[str, str]) -> tuple[str, ...]:
    """``-c`` settings that turn every configured filter driver into a no-op.

    Reading configuration runs nothing, so the driver names are read first and
    each driver's ``smudge``, ``clean`` and ``process`` are emptied.
    """
    key = f"{cwd}|{env.get('GIT_DIR', '')}"
    if key not in _FILTERS:
        base = ["git", *(arg for item in _SAFE_CONFIG for arg in ("-c", item))]
        result = _run([*base, "config", "--name-only", "--get-regexp", r"^filter\."], cwd, env, 30)
        drivers = sorted({line.split(".")[1] for line in result.stdout.splitlines()
                          if line.count(".") >= 2 and line.startswith("filter.")})
        out = []
        for driver in drivers:
            for part in ("smudge", "clean", "process"):
                out += ["-c", f"filter.{driver}.{part}="]
            out += ["-c", f"filter.{driver}.required=false"]
        _FILTERS[key] = tuple(out)
    return _FILTERS[key]


def git(args: list[str], cwd: Path, *, timeout: float = _GIT_TIMEOUT_S, check: bool = True,
        env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    """Run ``git`` with nothing from the repo executed; stdout, stripped.

    Raises ``WorktreeError("git_failed")`` when ``check`` and git fails.
    """
    full_env = _git_env(env)
    command = ["git", *(arg for item in _SAFE_CONFIG for arg in ("-c", item)),
               *_filter_overrides(Path(cwd), full_env), *args]
    try:
        result = subprocess.run(command, cwd=str(cwd), env=full_env, capture_output=True, text=True,
                                timeout=timeout, check=False, input=stdin)
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
    objects: Path  # the task's own object folder; the repo's store is a read-only alternate

    @property
    def write_paths(self) -> tuple[Path, ...]:
        """Write grants: the worktree, its own object folder, its admin folder and the
        ``openswap/`` branch namespace (refs and their logs; git writes a sibling
        ``.lock`` file to update a ref, so the namespace folder, not the ref file).
        Never the repo's shared object store, config, hooks or other refs."""
        namespace = Path("refs") / "heads" / BRANCH_PREFIX
        return (self.path, self.objects, self.git_dir,
                self.common_dir / namespace, self.common_dir / "logs" / namespace)

    @property
    def read_paths(self) -> tuple[Path, ...]:
        """Read grants beyond the worktree: the repo's shared ``.git`` only, never its working copy."""
        return (self.common_dir,)

    def env(self) -> dict[str, str]:
        """The task's git: new objects to its own folder, the repo's as alternates."""
        return {"GIT_OBJECT_DIRECTORY": str(self.objects),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.common_dir / "objects")}

    def worker_env(self) -> dict[str, str]:
        """The worker's git on this worktree: from the record, never the task-writable ``.git`` file."""
        return {"GIT_DIR": str(self.git_dir), "GIT_WORK_TREE": str(self.path), **self.env()}

    def to_dict(self) -> dict:
        return {"path": str(self.path), "repo": str(self.repo), "common_dir": str(self.common_dir),
                "git_dir": str(self.git_dir), "branch": self.branch, "objects": str(self.objects)}

    @classmethod
    def from_dict(cls, data: dict) -> "Worktree":
        return cls(Path(data["path"]), Path(data["repo"]), Path(data["common_dir"]), Path(data["git_dir"]),
                   str(data["branch"]), Path(data["objects"]))


def _record_path(path: Path) -> Path:
    """The worker's record of a task's worktree: beside it, where the task cannot write."""
    return Path(path).parent / f"{Path(path).name}.json"


def load_record(path: Path) -> Worktree | None:
    try:
        return Worktree.from_dict(json.loads(_record_path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remove_tree(path: Path) -> None:
    """``shutil.rmtree`` that also removes read-only files (git's objects on Windows)."""
    import shutil
    import stat
    import sys

    def retry(function, target, _info):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(target)
        except OSError:
            pass

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
    owner's copy are not carried over (and are never touched). The checkout
    runs no filters, hooks or fsmonitor (see the module notes).
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
    objects = dest.parent / f"{job_id}.objects"
    if os.path.lexists(dest) or os.path.lexists(objects):
        raise WorktreeError("worktree_exists")
    try:
        results_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_dirs(dest.parent, results_root)
        objects.mkdir(mode=0o700)
        (objects / "info").mkdir(mode=0o700)
        (objects / "pack").mkdir(mode=0o700)
    except OSError:
        raise WorktreeError("worktree_unavailable") from None
    branch = f"{BRANCH_PREFIX}/{job_id[:8]}"
    if git(["branch", "--list", branch], repo, timeout=30):
        branch = f"{BRANCH_PREFIX}/{job_id}"
    try:
        git(["worktree", "add", "--quiet", "-b", branch, str(dest), "HEAD"], repo)
    except WorktreeError:
        remove_tree(objects)
        raise
    try:
        # The branch namespace's log folder exists even where reflogs are off,
        # so the sandbox grant names a real folder.
        common = common_dir(repo)
        (common / "logs" / "refs" / "heads" / BRANCH_PREFIX).mkdir(parents=True, exist_ok=True)
        git_dir_text = git(["rev-parse", "--git-dir"], dest, timeout=30)
        git_dir = Path(git_dir_text)
        git_dir = pathid.canonical(git_dir if git_dir.is_absolute() else dest / git_dir)
        os.chmod(dest, 0o700)
        tree = Worktree(pathid.canonical(dest), repo, common, git_dir, branch, pathid.canonical(objects))
        record = _record_path(dest)
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(tree.to_dict(), stream)
    except (WorktreeError, OSError):
        remove(dest, force=True)
        raise WorktreeError("worktree_unavailable") from None
    return tree


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


def task_env(repo: Path, tree: Worktree | None = None) -> dict[str, str]:
    """Environment for the task's own git: no background maintenance, the repo's
    identity, and (for a worktree) its own object folder."""
    env = {"GIT_CONFIG_COUNT": str(len(GIT_TASK_CONFIG))}
    for index, (key, value) in enumerate(GIT_TASK_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env.update(identity(repo))
    env.setdefault("GIT_AUTHOR_NAME", "OpenSwap task")
    env.setdefault("GIT_COMMITTER_NAME", "OpenSwap task")
    env.setdefault("GIT_AUTHOR_EMAIL", "openswap-task@localhost")
    env.setdefault("GIT_COMMITTER_EMAIL", "openswap-task@localhost")
    if tree is not None:
        env.update(tree.env())
    return env


def intact(tree: Worktree) -> bool:
    """Whether the task left its admin folder pointing at its own repo and branch.

    The admin folder is task-writable; the worker runs git there only if its
    ``commondir`` still names the recorded repo, its ``gitdir`` the recorded
    worktree, and ``HEAD`` the task's own branch.
    """
    admin = Path(tree.git_dir)
    try:
        common_text = (admin / "commondir").read_text(encoding="utf-8").strip()
        gitdir_text = (admin / "gitdir").read_text(encoding="utf-8").strip()
        head = (admin / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    common = Path(common_text) if Path(common_text).is_absolute() else admin / common_text
    return (pathid.same(common, tree.common_dir) and pathid.same(Path(gitdir_text).parent, tree.path)
            and head == f"ref: refs/heads/{tree.branch}")


def _object_format(tree: Worktree) -> str:
    try:
        return git(["rev-parse", "--show-object-format"], tree.repo, timeout=30, check=False) or "sha1"
    except WorktreeError:
        return "sha1"


def import_objects(tree: Worktree) -> int:
    """Copy the task's new loose objects into the repo's store; each verified against its name.

    A file whose content does not hash to its name is skipped, and nothing
    already in the store is overwritten, so a task can never plant a bad
    copy of an object the owner will need. Returns how many were imported.
    """
    algorithm = "sha256" if _object_format(tree) == "sha256" else "sha1"
    target_root = Path(tree.common_dir) / "objects"
    imported = 0
    try:
        folders = [p for p in Path(tree.objects).iterdir() if len(p.name) == 2 and p.is_dir() and not p.is_symlink()]
    except OSError:
        return 0
    for folder in folders:
        try:
            files = [p for p in folder.iterdir() if p.is_file() and not p.is_symlink()]
        except OSError:
            continue
        for path in files:
            name = folder.name + path.name
            target = target_root / folder.name / path.name
            if os.path.lexists(target):
                continue
            try:
                if path.stat().st_size > _MAX_OBJECT_BYTES:
                    continue
                data = path.read_bytes()
                if hashlib.new(algorithm, zlib.decompress(data)).hexdigest() != name:
                    continue
                target.parent.mkdir(exist_ok=True)
                temporary = target.parent / f".openswap-import-{path.name}"
                temporary.write_bytes(data)
                os.chmod(temporary, 0o444)
                os.replace(temporary, target)
                imported += 1
            except (OSError, zlib.error, ValueError):
                continue
    return imported


def finish(tree: Worktree, message: str) -> bool:
    """After the task: import its objects, commit what it left uncommitted, keep the branch.

    Returns whether the worktree is now clean (so it may be removed). Does
    nothing when the task repointed its admin folder or switched branches.
    """
    if not intact(tree):
        return False
    import_objects(tree)
    env = {**tree.worker_env(), **task_env(tree.repo)}
    # The worker's own commit goes straight to the repo's store.
    env.pop("GIT_OBJECT_DIRECTORY", None)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(tree.objects)
    try:
        if git(["status", "--porcelain", "--ignore-submodules=all"], tree.path, env=env, timeout=120):
            git(["add", "-A"], tree.path, env=env)
            git(["commit", "-q", "--no-verify", "-m", message], tree.path, env=env)
        # Everything the branch needs must now be in the repo's own store.
        git(["rev-list", "--objects", "--quiet", tree.branch], tree.repo, timeout=120,
            env={"GIT_DIR": str(tree.common_dir)})
        return not git(["status", "--porcelain", "--ignore-submodules=all"], tree.path, env=env, timeout=120)
    except WorktreeError:
        return False


def remove(path: Path, *, force: bool = False) -> bool:
    """Remove a worktree directory, its admin entry and its object folder; its branch is kept.

    Uses the worker's record, never the task-writable ``.git`` file. Without
    ``force`` a worktree that is not intact or not clean is kept.
    """
    path = Path(path)
    tree = load_record(path)
    repo_ok = tree is not None and tree.repo.exists()
    if not force:
        if tree is None or not repo_ok or not intact(tree) or is_dirty(tree) is not False:
            return False
    if repo_ok and intact(tree):
        # Twice --force also removes a locked worktree.
        args = ["worktree", "remove", *(["--force", "--force"] if force else []), str(path)]
        try:
            git(args, tree.repo)
        except WorktreeError:
            if not force:
                return False
    if path.exists():
        remove_tree(path)
    if tree is not None:
        if Path(tree.objects).exists():
            remove_tree(tree.objects)
        if repo_ok:
            try:
                git(["worktree", "prune"], tree.repo, timeout=60, check=False)
            except WorktreeError:
                pass
    try:
        _record_path(path).unlink()
    except OSError:
        pass
    return not path.exists()


def is_dirty(tree: Worktree | Path) -> bool | None:
    """Whether a worktree holds uncommitted or untracked work (None when git cannot tell)."""
    if not isinstance(tree, Worktree):
        tree = load_record(tree)
    if tree is None or not tree.repo.exists() or not intact(tree):
        return None
    try:
        return bool(git(["status", "--porcelain", "--ignore-submodules=all"], tree.path,
                        env=tree.worker_env(), timeout=60))
    except WorktreeError:
        return None


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
    intact: bool = True


def _locked(tree: Worktree | None) -> bool:
    return tree is not None and os.path.lexists(Path(tree.git_dir) / "locked")


def list_all(results_root: Path) -> list[TaskWorktree]:
    root = worktrees_root(results_root)
    out = []
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    for folder in folders:
        try:
            tasks = sorted(p for p in folder.iterdir()
                           if p.is_dir() and not p.is_symlink() and not p.name.endswith(".objects"))
        except OSError:
            continue
        for task in tasks:
            tree = load_record(task)
            present = tree is not None and tree.repo.exists()
            whole = present and intact(tree)
            out.append(TaskWorktree(
                folder.name, task.name, task, tree.repo if tree is not None else None,
                tree.branch if tree is not None else None,
                is_dirty(tree) if whole else None,
                _locked(tree),
                whole or not present,
            ))
    return out


def sweep(results_root: Path, finished, *, force: bool = False) -> list[TaskWorktree]:
    """Remove the worktrees of finished tasks: clean ones, or every one with ``force``.

    ``finished(job_id)`` says whether a task has ended. A dirty, locked,
    tampered or unreadable worktree is kept unless ``force``; one whose repo
    was moved or deleted is removed (there is nothing left to inspect it
    with). Returns the ones removed. Branches are always kept.
    """
    removed = []
    for item in list_all(results_root):
        if not finished(item.job_id):
            continue
        repo_gone = item.repo is None or not item.repo.exists()
        if not force and not repo_gone and (item.dirty is not False or item.locked or not item.intact):
            continue
        if remove(item.path, force=force or repo_gone):
            removed.append(item)
            try:
                item.path.parent.rmdir()  # the folder's directory, once it is empty
            except OSError:
                pass
    return removed
