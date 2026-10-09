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
checkout and other tasks use. When the task ends the worker commits
anything left uncommitted for the task's branch, imports only the objects
that commit needs (through ``git pack-objects`` and ``git index-pack
--strict``), then moves the branch and keeps it. A clean worktree is
then removed; one the worker could not finish is kept for inspection.
``openswap worker worktrees`` lists them and ``prune`` removes them.

Every git command here runs as the worker, outside the task's sandbox. So
none may run code from the repo or from anything the task could write:
hooks are disabled (``core.hooksPath``), as are checkout and clean filters
(every driver any config file or include could define, whatever the
include's condition),
``core.fsmonitor``, commit signing and submodule recursion; and git is
pointed at the worktree with explicit ``GIT_DIR``/``GIT_WORK_TREE`` from a
record the task cannot write, after checking the task did not repoint its
admin folder.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from openswap import pathid

WORKTREES_DIR = ".worktrees"
BRANCH_PREFIX = "openswap"
_GIT_TIMEOUT_S = 300
_MAX_CHILD_REPOS = 64
_MAX_SCANNED_CHILDREN = 5000
_MAX_PACK_BYTES = 2 * 1024 * 1024 * 1024
# The task's own git must never start background maintenance that rewrites
# packs or refs outside the paths it may write.
GIT_TASK_CONFIG = (("gc.auto", "0"), ("maintenance.auto", "false"))
# Settings under which git runs nothing from the repo or from its config.
_SAFE_CONFIG = (
    f"core.hooksPath={os.devnull}", "core.fsmonitor=false", "submodule.recurse=false",
    "commit.gpgSign=false", "tag.gpgSign=false", "core.sshCommand=false", "protocol.allow=never",
    # Never the owner's (or a task's) sparse-checkout rules: a task's copy is a
    # full checkout, and the worker's private index sees every path.
    "core.sparseCheckout=false", "index.sparse=false",
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


_MAX_CONFIG_FILES = 64


def _is_include(key: str) -> bool:
    return key == "include.path" or (key.startswith("includeif.") and key.endswith(".path"))


def _driver(key: str) -> str | None:
    """``name`` for a ``filter.<name>.<setting>`` key (a name may hold dots or ``=``)."""
    if not key.startswith("filter."):
        return None
    return key[len("filter."):].rpartition(".")[0] or None


def _include_target(value: str, including: Path, env: dict[str, str]) -> Path:
    if value == "~" or value.startswith("~/"):
        return Path(env.get("HOME") or Path.home()) / value[2:]
    target = Path(value)
    return target if target.is_absolute() else including.parent / target


def _configured_drivers(cwd: Path, env: dict[str, str], base: list[str]) -> set[str]:
    """Every filter driver any config git may read here could define, whatever its conditions.

    Conditional includes (``includeIf "onbranch:…"``, ``"gitdir:…"``,
    ``"hasconfig:…"``) are judged per command: one inactive now may be
    active for the next (``worktree add -b openswap/…`` switches branch
    under a child git). So every config file git reads is listed, and each
    include in it is followed regardless of its condition, as raw files
    (``--no-includes``). Reading configuration runs nothing.
    """
    listing = _run([*base, "config", "--list", "--show-origin", "--name-only", "--null"], cwd, env, 30)
    fields = listing.stdout.split("\0")
    drivers, pending, seen = set(), [], set()
    for origin, key in zip(fields[0::2], fields[1::2]):
        name = _driver(key)
        if name:
            drivers.add(name)
        if origin.startswith("file:"):
            path = Path(origin[len("file:"):])
            pending.append(path if path.is_absolute() else Path(cwd) / path)
    while pending and len(seen) < _MAX_CONFIG_FILES:
        path = pending.pop()
        try:
            key_path = os.path.normcase(os.path.abspath(path))
        except (OSError, ValueError):
            continue
        if key_path in seen or not os.path.isfile(path):
            continue
        seen.add(key_path)
        raw = _run([*base, "config", "--file", str(path), "--no-includes", "--null", "--list"], cwd, env, 30)
        for entry in raw.stdout.split("\0"):
            key, _, value = entry.partition("\n")
            name = _driver(key)
            if name:
                drivers.add(name)
            elif _is_include(key) and value:
                pending.append(_include_target(value, path, env))
    return drivers


def _filter_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    """Settings that turn every filter driver git could find here into a no-op.

    The driver names are read first (again for every command: the owner may
    add one at any time), from every config file and every include,
    conditional or not (see ``_configured_drivers``), and each driver's
    ``smudge``, ``clean`` and ``process`` are emptied. These settings come
    from the environment, which git ranks above every config file and every
    include. A driver name may contain dots or ``=``: it is everything
    between ``filter.`` and the last dot, and the settings travel as
    ``GIT_CONFIG_KEY_n``/``VALUE_n`` pairs, never as ``-c name=value`` (which
    splits at the first ``=``).
    """
    base = ["git", *(arg for item in _SAFE_CONFIG for arg in ("-c", item))]
    out = []
    for driver in sorted(_configured_drivers(Path(cwd), env, base)):
        for part in ("smudge", "clean", "process"):
            out.append((f"filter.{driver}.{part}", ""))
        out.append((f"filter.{driver}.required", "false"))
    return out


def _with_config(env: dict[str, str], settings: list[tuple[str, str]]) -> dict[str, str]:
    """``env`` with ``settings`` appended to its ``GIT_CONFIG_COUNT`` entries."""
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    out = dict(env)
    for key, value in settings:
        out[f"GIT_CONFIG_KEY_{count}"] = key
        out[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    out["GIT_CONFIG_COUNT"] = str(count)
    return out


def git(args: list[str], cwd: Path, *, timeout: float = _GIT_TIMEOUT_S, check: bool = True,
        env: dict[str, str] | None = None, stdin: str | None = None) -> str:
    """Run ``git`` with nothing from the repo executed; stdout, stripped.

    Raises ``WorktreeError("git_failed")`` when ``check`` and git fails.
    """
    full_env = _git_env(env)
    full_env = _with_config(full_env, _filter_overrides(Path(cwd), full_env))
    command = ["git", *(arg for item in _SAFE_CONFIG for arg in ("-c", item)), *args]
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


def child_repos(parent: Path, keep: set[str] | frozenset[str] = frozenset()) -> list[Path]:
    """The git repos directly inside ``parent`` (not hidden, not symlinks), by name.

    At most ``_MAX_CHILD_REPOS`` new ones; a repo whose path is in ``keep``
    (already offered under an ID) is listed whatever the cap, so repos added
    later can never push it out.
    """
    out = []
    new = 0
    try:
        children = sorted(Path(parent).iterdir(), key=lambda p: (p.name.lower(), p.name))
    except OSError:
        return []
    def usable(child: Path) -> bool:
        try:
            return (not child.name.startswith(".") and not child.is_symlink() and child.is_dir()
                    and os.path.lexists(child / ".git"))
        except OSError:
            return False

    for child in children[:_MAX_SCANNED_CHILDREN]:
        if not usable(child):
            continue
        repo = pathid.canonical(child)
        if str(repo) in keep:
            out.append(repo)
        elif new < _MAX_CHILD_REPOS:
            out.append(repo)
            new += 1
    # Repos already offered are checked by path, apart from the scan limits,
    # so neither cap can drop one a queued task may still name.
    parent_path = pathid.canonical(parent)
    for text in keep:
        repo = Path(text)
        if repo in out or not pathid.same(repo.parent, parent_path) or not usable(repo):
            continue
        out.append(pathid.canonical(repo))
    return sorted(out, key=lambda p: (p.name.lower(), p.name))


def common_dir(repo: Path) -> Path:
    """The repo's shared ``.git`` directory (objects, refs, config)."""
    text = git(["rev-parse", "--git-common-dir"], repo, timeout=30)
    path = Path(text)
    return pathid.canonical(path if path.is_absolute() else Path(repo) / path)


def alternate_path(path: Path) -> str:
    """One entry for ``GIT_ALTERNATE_OBJECT_DIRECTORIES``, a colon-separated list:
    C-quoted when the path holds a ``:`` or a quote (both legal in macOS names)."""
    text = str(path)
    if os.name != "nt" and (any(char in text for char in ':"\\') or text.startswith('"')):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


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
        """Write grants: the worktree, its own object folder and its admin folder (index,
        HEAD). No ref at all: the task cannot commit, move or delete any branch, its own
        or another task's; the worker commits its work to its branch when it ends.
        Never the repo's shared object store, config, hooks or refs."""
        return (self.path, self.objects, self.git_dir)

    @property
    def read_paths(self) -> tuple[Path, ...]:
        """Read grants beyond the worktree: the repo's shared ``.git`` only, never its working copy."""
        return (self.common_dir,)

    def env(self) -> dict[str, str]:
        """The task's git: new objects to its own folder, the repo's as alternates."""
        return {"GIT_OBJECT_DIRECTORY": str(self.objects),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate_path(self.common_dir / "objects")}

    @property
    def available(self) -> bool:
        """Whether the repo's shared ``.git`` is still there (the approved working copy
        may be gone, as when it was itself a linked worktree, while the repo is not)."""
        return Path(self.common_dir).is_dir()

    def repo_env(self) -> dict[str, str]:
        return {"GIT_DIR": str(self.common_dir)}

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
        if os.path.lexists(objects):
            remove_tree(objects)
        raise WorktreeError("worktree_unavailable") from None
    # Nothing of this task may stay behind, or a retry finds `worktree_exists`.
    try:
        branch = _free_branch(repo, job_id)
    except WorktreeError:
        remove_tree(objects)
        raise
    try:
        # No checkout here: `worktree add` checks out in a child git whose
        # config is read on the new branch, after the filter scan above ran
        # on the owner's. The checkout runs below as a command of its own.
        git(["worktree", "add", "--quiet", "--no-checkout", "-b", branch, str(dest), "HEAD"], repo)
    except WorktreeError:
        # `-b` may have made the branch before the checkout failed: it was
        # free a moment ago, so it is this task's to delete.
        _undo_create(repo, dest, objects, branch)
        raise
    try:
        common = common_dir(repo)
        git_dir_text = git(["rev-parse", "--git-dir"], dest, timeout=30)
        git_dir = Path(git_dir_text)
        git_dir = pathid.canonical(git_dir if git_dir.is_absolute() else dest / git_dir)
        # `worktree add` copies the owner's per-worktree config and sparse
        # rules; the task's copy is full and has neither.
        for copied in (git_dir / "config.worktree", git_dir / "info" / "sparse-checkout"):
            if os.path.lexists(copied):
                os.unlink(copied)
        # Now in the new worktree, on the task's branch: config (and any
        # include that branch turns on) is read again for this command.
        git(["reset", "--hard", "--quiet"], dest, env={"GIT_DIR": str(git_dir), "GIT_WORK_TREE": str(dest)})
        os.chmod(dest, 0o700)
        tree = Worktree(pathid.canonical(dest), repo, common, git_dir, branch, pathid.canonical(objects))
        record = _record_path(dest)
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(tree.to_dict(), stream)
    except (WorktreeError, OSError, ValueError):
        _undo_create(repo, dest, objects, branch)
        raise WorktreeError("worktree_unavailable") from None
    return tree


def _undo_create(repo: Path, dest: Path, objects: Path, branch: str) -> None:
    """Remove a worktree whose setup failed, from the paths already known (no record yet)."""
    for args in (["worktree", "remove", "--force", "--force", str(dest)], ["worktree", "prune"],
                 ["branch", "-D", branch]):
        try:
            git(args, repo, timeout=60, check=False)
        except WorktreeError:
            pass
    for leftover in (dest, objects):
        if os.path.lexists(leftover):
            remove_tree(leftover)
    try:
        _record_path(dest).unlink()
    except OSError:
        pass


def _free_branch(repo: Path, job_id: str) -> str:
    """``openswap/<short id>``, or a name no existing branch blocks.

    A branch ``openswap`` (or ``openswap/<short id>/…``) would block the
    usual name as a file/folder clash in ``refs/heads``, so the fallbacks are
    the full ID, then the flat ``openswap-<id>`` forms. Names are compared
    ignoring case: on a case-insensitive volume (the macOS default) a loose
    ``OpenSwap`` branch blocks ``openswap/<id>`` just the same, and a name
    that differs only in case would be the same file. Treating them as taken
    on a case-sensitive volume only costs a fallback name.
    """
    # Full names: a short name turns into `heads/<name>` when a tag shares it.
    existing = {line[len("refs/heads/"):].casefold() for line in git(
        ["for-each-ref", "--format=%(refname)", "refs/heads/"], repo, timeout=30).splitlines()
        if line.startswith("refs/heads/")}

    def free(name: str) -> bool:
        name = name.casefold()
        parts = name.split("/")
        prefixes = {"/".join(parts[:i]) for i in range(1, len(parts))}
        return name not in existing and not (prefixes & existing) and not any(
            other.startswith(name + "/") for other in existing)

    for name in (f"{BRANCH_PREFIX}/{job_id[:8]}", f"{BRANCH_PREFIX}/{job_id}",
                 f"{BRANCH_PREFIX}-{job_id[:8]}", f"{BRANCH_PREFIX}-{job_id}"):
        if free(name):
            return name
    raise WorktreeError("branch_unavailable")


def identity(repo: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """The repo's commit identity as git environment variables (empty when unset).

    The task's sandbox may not read ``~/.gitconfig``, so the worker reads it
    here and hands it over; without one git would refuse to commit.
    """
    out = {}
    for key, variables in (("user.name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                           ("user.email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        try:
            value = git(["config", "--get", key], repo, timeout=30, check=False, env=env)
        except WorktreeError:
            value = ""
        if value and "\n" not in value and len(value) <= 200:
            for variable in variables:
                out[variable] = value
    return out


def task_env(repo: Path, tree: Worktree | None = None, *, git_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for the task's own git: no background maintenance, the repo's
    identity, and (for a worktree) its own object folder."""
    env = {"GIT_CONFIG_COUNT": str(len(GIT_TASK_CONFIG))}
    for index, (key, value) in enumerate(GIT_TASK_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env.update(identity(repo, git_env))
    env.setdefault("GIT_AUTHOR_NAME", "OpenSwap task")
    env.setdefault("GIT_COMMITTER_NAME", "OpenSwap task")
    env.setdefault("GIT_AUTHOR_EMAIL", "openswap-task@localhost")
    env.setdefault("GIT_COMMITTER_EMAIL", "openswap-task@localhost")
    if tree is not None:
        env.update(tree.env())
    return env


def _made_sparse(tree: Worktree) -> bool:
    """Whether the task gave its checkout sparse rules or config of its own.

    A sparse checkout leaves paths out of the folder that are still on the
    branch, which the worker's private index would read as deletions. Such a
    worktree is never committed or removed by the worker.
    """
    admin = Path(tree.git_dir)
    return any(os.path.lexists(admin / name) for name in ("config.worktree", "info/sparse-checkout"))


def intact(tree: Worktree) -> bool:
    """Whether the task left its admin folder pointing at its own repo and branch.

    The admin folder is task-writable; the worker runs git there only if its
    ``commondir`` still names the recorded repo, its ``gitdir`` the recorded
    worktree, and ``HEAD`` the task's own branch.
    """
    admin = Path(tree.git_dir)
    try:
        texts = []
        for name in ("commondir", "gitdir", "HEAD"):
            path = admin / name
            # Regular files only: never follow a link (or block on a FIFO) the task planted.
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return False
            texts.append(path.read_text(encoding="utf-8").strip())
        common_text, gitdir_text, head = texts
    except (OSError, UnicodeDecodeError):
        return False
    common = Path(common_text) if Path(common_text).is_absolute() else admin / common_text
    return (pathid.same(common, tree.common_dir) and pathid.same(Path(gitdir_text).parent, tree.path)
            and head == f"ref: refs/heads/{tree.branch}")


def import_objects(tree: Worktree, commit: str | None = None) -> bool:
    """Copy into the repo's store the objects ``commit`` (default: the branch) needs and lacks.

    Only those objects, never anything else the task left in its object
    folder, and only through git's own checks: ``rev-list`` names what is
    missing (stopping at every other ref and the owner's ``HEAD``),
    ``pack-objects`` packs it from the repo's store plus the task's folder,
    and ``index-pack --strict`` checks every object (its header, its hash
    and, as ``fsck`` would, its content) as it would a fetched pack before
    anything lands. Returns whether all of it is now in the repo's store.
    """
    commit = commit or f"refs/heads/{tree.branch}"
    read_env = _read_env(tree)
    try:
        names = git(["rev-list", "--objects", "--no-object-names", "--single-worktree", commit, "--not",
                     f"--exclude=refs/heads/{tree.branch}", "--all"], tree.common_dir, timeout=_GIT_TIMEOUT_S,
                    env=read_env)
    except WorktreeError:
        return False
    if not names:
        return True
    safe = [arg for item in _SAFE_CONFIG for arg in ("-c", item)]
    try:
        read_full = _with_config(_git_env(read_env), _filter_overrides(Path(tree.common_dir), _git_env(read_env)))
        store_env = _git_env({"GIT_DIR": str(tree.common_dir)})
        store_full = _with_config(store_env, _filter_overrides(Path(tree.common_dir), store_env))
    except WorktreeError:
        return False
    try:
        with tempfile.TemporaryFile() as listing:
            listing.write(names.encode() + b"\n")
            listing.seek(0)
            packer = subprocess.Popen(["git", *safe, "pack-objects", "--stdout", "-q"], cwd=str(tree.common_dir),
                                      stdin=listing, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      env=read_full)
            try:
                # Streamed from git to git: never held in the worker's memory.
                indexed = subprocess.run(
                    ["git", *safe, "index-pack", "--stdin", "--strict", f"--max-input-size={_MAX_PACK_BYTES}"],
                    cwd=str(tree.common_dir), stdin=packer.stdout, capture_output=True, timeout=_GIT_TIMEOUT_S,
                    check=False, env=store_full)
            finally:
                packer.stdout.close()
                try:
                    packer.wait(timeout=_GIT_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    packer.kill()
                    packer.wait()
    except (OSError, subprocess.SubprocessError):
        return False
    if packer.returncode != 0 or indexed.returncode != 0:
        return False
    try:
        git(["rev-list", "--objects", "--quiet", commit], tree.common_dir, timeout=_GIT_TIMEOUT_S,
            env=tree.repo_env())
    except WorktreeError:
        return False
    return True


def _private_env(tree: Worktree, index: Path) -> dict[str, str]:
    """The worker's git on a task's checkout without its admin folder.

    The admin folder (HEAD, index, logs, COMMIT_EDITMSG) is task-writable, so
    a link planted there could make an unsandboxed git write anywhere. The
    worker uses the repo's shared ``.git`` (which the task cannot write), the
    checkout as the work tree, and an index of its own.
    """
    return {**_read_env(tree), "GIT_WORK_TREE": str(tree.path), "GIT_INDEX_FILE": str(index)}


def _read_env(tree: Worktree) -> dict[str, str]:
    """Reading (never writing) the repo's store plus the task's own objects."""
    return {"GIT_DIR": str(tree.common_dir), "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate_path(tree.objects)}


def _tip(tree: Worktree) -> str:
    return git(["rev-parse", "--verify", "--quiet", f"refs/heads/{tree.branch}^{{commit}}"], tree.common_dir,
               timeout=30, env=_read_env(tree))


def _pending(tree: Worktree, scratch: Path) -> tuple[str, str, str]:
    """``(tip, tip tree, checkout tree)``: the branch tip and the tree the checkout holds now."""
    env = _private_env(tree, scratch / "index")
    tip = _tip(tree)
    git(["read-tree", tip], tree.common_dir, timeout=120, env=env)
    git(["add", "-A", "--", "."], tree.path, timeout=_GIT_TIMEOUT_S, env=env)
    current = git(["write-tree"], tree.common_dir, timeout=120, env=env)
    return tip, git(["rev-parse", f"{tip}^{{tree}}"], tree.common_dir, timeout=30, env=_read_env(tree)), current


def _ignored(tree: Worktree, env: dict[str, str]) -> bool:
    """Whether the checkout holds files the repo's ignore rules leave out of a commit.

    A fresh checkout has none, so any there are the task's (build output,
    ``.env`` files, logs). They are never committed, but they keep the
    worktree for the owner to look at.
    """
    return bool(git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], tree.path,
                    timeout=_GIT_TIMEOUT_S, env=env))


def finish(tree: Worktree, message: str) -> bool:
    """After the task: commit what it left on its branch, import what that needs, keep the branch.

    Returns whether the branch now holds everything in the checkout (so the
    worktree may be removed): not when the task left ignored files, which are
    never committed. Does nothing when the task repointed its admin
    folder or switched branches. Never reads or writes the task-writable
    admin folder beyond the checks in ``intact``: the commit is built in a
    private index and the branch moved with ``update-ref`` in the shared
    ``.git``.
    """
    if not intact(tree) or _made_sparse(tree):
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="openswap-finish-") as scratch:
            tip, tip_tree, current = _pending(tree, Path(scratch))
            commit = tip
            if current != tip_tree:
                identity_env = {k: v for k, v in task_env(tree.common_dir, git_env=tree.repo_env()).items()
                                if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
                commit = git(["commit-tree", current, "-p", tip, "-m", message], tree.common_dir, timeout=60,
                             env={**_private_env(tree, Path(scratch) / "index"), **identity_env})
            # The branch moves only once everything it will need is in the repo's store.
            if not import_objects(tree, commit):
                return False
            if commit != tip:
                git(["update-ref", "-m", message, f"refs/heads/{tree.branch}", commit, tip], tree.common_dir,
                    timeout=60, env=tree.repo_env())
            leftover = _ignored(tree, _private_env(tree, Path(scratch) / "index"))
        # Everything the branch needs must now be in the repo's own store
        # (by its full name: a tag of the same name never stands in for it).
        git(["rev-list", "--objects", "--quiet", f"refs/heads/{tree.branch}"], tree.common_dir, timeout=120,
            env=tree.repo_env())
    except (WorktreeError, OSError):
        return False
    if leftover:
        return False  # the branch has the rest; the worktree stays for the ignored files
    # Finished: a later sweep may remove it.
    try:
        (Path(tree.path).parent / f"{Path(tree.path).name}.finished").touch()
    except OSError:
        return False
    return True


def remove(path: Path, *, force: bool = False) -> bool:
    """Remove a worktree directory, its admin entry and its object folder; its branch is kept.

    Uses the worker's record, never the task-writable ``.git`` file, and runs
    no git inside the checkout: the folders are deleted (links are removed,
    never followed) and ``git worktree prune`` drops the registration.
    Without ``force`` a worktree that is not intact or holds work not on its
    branch is kept.
    """
    path = Path(path)
    tree = load_record(path)
    repo_ok = tree is not None and tree.available
    if not force:
        if tree is None or not repo_ok or not intact(tree) or is_dirty(tree) is not False:
            return False
    if path.exists() or path.is_symlink():
        remove_tree(path)
    if tree is not None:
        if Path(tree.objects).exists():
            remove_tree(tree.objects)
        admin = Path(tree.git_dir)
        if repo_ok and pathid.inside(admin, Path(tree.common_dir) / "worktrees") and not admin.is_symlink() \
                and admin.is_dir():
            remove_tree(admin)
        if repo_ok:
            try:
                git(["worktree", "prune"], tree.common_dir, timeout=60, check=False, env=tree.repo_env())
            except WorktreeError:
                pass
    for leftover in (_record_path(path), path.parent / f"{path.name}.finished"):
        try:
            leftover.unlink()
        except OSError:
            pass
    return not path.exists()


def is_dirty(tree: Worktree | Path) -> bool | None:
    """Whether a checkout holds work not on its branch, ignored files included (None when git cannot tell).

    Read through a private index (see ``_private_env``), never the
    task-writable admin folder.
    """
    if not isinstance(tree, Worktree):
        tree = load_record(tree)
    if tree is None or not tree.available or not intact(tree) or _made_sparse(tree):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="openswap-status-") as scratch:
            env = _private_env(tree, Path(scratch) / "index")
            git(["read-tree", _tip(tree)], tree.common_dir, timeout=120, env=env)
            git(["update-index", "-q", "--refresh"], tree.path, timeout=120, env=env, check=False)
            changed = git(["diff-files", "--name-only"], tree.path, timeout=120, env=env)
            untracked = git(["ls-files", "--others", "--exclude-standard"], tree.path, timeout=120, env=env)
            return bool(changed or untracked) or _ignored(tree, env)
    except (WorktreeError, OSError):
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
    available: bool = True  # the repo's shared .git is still there


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
            present = tree is not None and tree.available
            whole = present and intact(tree)
            out.append(TaskWorktree(
                folder.name, task.name, task, tree.repo if tree is not None else None,
                tree.branch if tree is not None else None,
                is_dirty(tree) if whole else None,
                _locked(tree),
                whole or not present,
                present,
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
        repo_gone = not item.available
        tree = load_record(item.path)
        if not repo_gone and tree is not None:
            # A task stopped by Stop, a timeout or a restart never reached the
            # adapter's own finish: import and commit its work first.
            done = (Path(item.path).parent / f"{item.job_id}.finished").exists() or finish(
                tree, f"OpenSwap task {item.job_id[:8]}: work left uncommitted")
            if force:
                import_objects(tree)  # never leave the branch pointing at deleted objects
            elif not done or item.locked:
                continue
        if remove(item.path, force=force or repo_gone):
            removed.append(item)
            try:
                item.path.parent.rmdir()  # the folder's directory, once it is empty
            except OSError:
                pass
    return removed
