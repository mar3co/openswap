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
import stat
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from openswap import pathid

WORKTREES_DIR = ".worktrees"
BRANCH_PREFIX = "openswap"
_GIT_TIMEOUT_S = 300
_MAX_CHILD_REPOS = 64
_MAX_SCANNED_CHILDREN = 5000
_MAX_OBJECT_BYTES = 512 * 1024 * 1024
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


def _filter_overrides(cwd: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    """Settings that turn every configured filter driver into a no-op.

    Reading configuration runs nothing, so the driver names are read first
    (again for every command: the owner may add one at any time) and each
    driver's ``smudge``, ``clean`` and ``process`` are emptied. A driver name
    may contain dots or ``=``: it is everything between ``filter.`` and the
    last dot, and the settings travel as ``GIT_CONFIG_KEY_n``/``VALUE_n``
    pairs, never as ``-c name=value`` (which splits at the first ``=``).
    """
    base = ["git", *(arg for item in _SAFE_CONFIG for arg in ("-c", item))]
    result = _run([*base, "config", "--null", "--name-only", "--get-regexp", r"^filter\."], cwd, env, 30)
    drivers = sorted({name[len("filter."):].rpartition(".")[0] for name in result.stdout.split("\0")
                      if name.startswith("filter.") and "." in name[len("filter."):]})
    out = []
    for driver in drivers:
        if not driver:
            continue
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
        git(["worktree", "add", "--quiet", "-b", branch, str(dest), "HEAD"], repo)
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
    the full ID, then the flat ``openswap-<id>`` forms.
    """
    # Full names: a short name turns into `heads/<name>` when a tag shares it.
    existing = {line[len("refs/heads/"):] for line in git(
        ["for-each-ref", "--format=%(refname)", "refs/heads/"], repo, timeout=30).splitlines()
        if line.startswith("refs/heads/")}

    def free(name: str) -> bool:
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


def _object_format(tree: Worktree) -> str:
    try:
        return git(["rev-parse", "--show-object-format"], tree.common_dir, timeout=30, check=False,
                   env=tree.repo_env()) or "sha1"
    except WorktreeError:
        return "sha1"


def _verified(data: bytes, name: str, algorithm: str) -> bool:
    """Whether a loose object's compressed ``data`` hashes to ``name``, decompressing
    in bounded steps (a tiny file may expand to gigabytes)."""
    digest = hashlib.new(algorithm)
    stream = zlib.decompressobj()
    total = 0
    chunk = stream.decompress(data, 1 << 20)
    while True:
        total += len(chunk)
        if total > _MAX_OBJECT_BYTES:
            return False
        digest.update(chunk)
        if not stream.unconsumed_tail:
            break
        chunk = stream.decompress(stream.unconsumed_tail, 1 << 20)
    tail = stream.flush()
    if total + len(tail) > _MAX_OBJECT_BYTES or not stream.eof:
        return False
    digest.update(tail)
    return digest.hexdigest() == name


def _import_packs(tree: Worktree) -> bool:
    """Index each pack the task made (a ``git repack`` or ``gc``) into the repo's store.

    ``git index-pack`` checks every object as it would a fetched pack.
    Returns whether every pack was imported.
    """
    ok = True
    try:
        packs = sorted(p for p in (Path(tree.objects) / "pack").glob("*.pack") if p.is_file() and not p.is_symlink())
    except OSError:
        return False
    for pack in packs:
        try:
            if pack.stat().st_size > _MAX_PACK_BYTES:
                ok = False  # never fed to git; the worktree is kept for the owner
                continue
            command = ["git", *(arg for item in _SAFE_CONFIG for arg in ("-c", item)),
                       "index-pack", "--stdin", "--fix-thin", f"--max-input-size={_MAX_PACK_BYTES}"]
            # Streamed from the file: never read into the worker's memory.
            with open(pack, "rb") as stream:
                result = subprocess.run(command, cwd=str(tree.common_dir), stdin=stream, capture_output=True,
                                        timeout=_GIT_TIMEOUT_S, check=False,
                                        env=_git_env({"GIT_DIR": str(tree.common_dir)}))
            ok = ok and result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    return ok


def import_objects(tree: Worktree) -> int:
    """Copy the task's new loose objects into the repo's store; each verified against its name.

    A file whose content does not hash to its name is skipped, and nothing
    already in the store is overwritten, so a task can never plant a bad
    copy of an object the owner will need. Packs go through ``git
    index-pack``. Returns how many loose objects were imported.
    """
    algorithm = "sha256" if _object_format(tree) == "sha256" else "sha1"
    target_root = Path(tree.common_dir) / "objects"
    _import_packs(tree)
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
                if not _verified(data, name, algorithm):
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


def _private_env(tree: Worktree, index: Path) -> dict[str, str]:
    """The worker's git on a task's checkout without its admin folder.

    The admin folder (HEAD, index, logs, COMMIT_EDITMSG) is task-writable, so
    a link planted there could make an unsandboxed git write anywhere. The
    worker uses the repo's shared ``.git`` (which the task cannot write), the
    checkout as the work tree, and an index of its own.
    """
    return {"GIT_DIR": str(tree.common_dir), "GIT_WORK_TREE": str(tree.path), "GIT_INDEX_FILE": str(index),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": alternate_path(tree.objects)}


def _tip(tree: Worktree) -> str:
    return git(["rev-parse", "--verify", "--quiet", f"refs/heads/{tree.branch}^{{commit}}"], tree.common_dir,
               timeout=30, env=tree.repo_env())


def _pending(tree: Worktree, scratch: Path) -> tuple[str, str, str]:
    """``(tip, tip tree, checkout tree)``: the branch tip and the tree the checkout holds now."""
    env = _private_env(tree, scratch / "index")
    tip = _tip(tree)
    git(["read-tree", tip], tree.common_dir, timeout=120, env=env)
    git(["add", "-A", "--", "."], tree.path, timeout=_GIT_TIMEOUT_S, env=env)
    current = git(["write-tree"], tree.common_dir, timeout=120, env=env)
    return tip, git(["rev-parse", f"{tip}^{{tree}}"], tree.common_dir, timeout=30, env=tree.repo_env()), current


def _ignored(tree: Worktree, env: dict[str, str]) -> bool:
    """Whether the checkout holds files the repo's ignore rules leave out of a commit.

    A fresh checkout has none, so any there are the task's (build output,
    ``.env`` files, logs). They are never committed, but they keep the
    worktree for the owner to look at.
    """
    return bool(git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], tree.path,
                    timeout=_GIT_TIMEOUT_S, env=env))


def finish(tree: Worktree, message: str) -> bool:
    """After the task: import its objects, commit what it left on its branch, keep the branch.

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
    import_objects(tree)
    try:
        with tempfile.TemporaryDirectory(prefix="openswap-finish-") as scratch:
            tip, tip_tree, current = _pending(tree, Path(scratch))
            if current != tip_tree:
                identity_env = {k: v for k, v in task_env(tree.common_dir, git_env=tree.repo_env()).items()
                                if k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
                commit = git(["commit-tree", current, "-p", tip, "-m", message], tree.common_dir, timeout=60,
                             env={**tree.repo_env(), **identity_env})
                git(["update-ref", "-m", message, f"refs/heads/{tree.branch}", commit, tip], tree.common_dir,
                    timeout=60, env=tree.repo_env())
            leftover = _ignored(tree, _private_env(tree, Path(scratch) / "index"))
        # Everything the branch needs must now be in the repo's own store.
        git(["rev-list", "--objects", "--quiet", tree.branch], tree.common_dir, timeout=120,
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
