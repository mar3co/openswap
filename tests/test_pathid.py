"""Path identity: one folder named two ways is still one folder.

The identity tests run everywhere. The case-variant tests run only where the
temp directory's filesystem is case-insensitive (APFS by default), detected at
run time: there ``~/library`` *is* ``~/Library``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openswap import pathid
from openswap.settings import WorkerWorkspace, load_worker_settings, set_worker_workspaces
from openswap.worker import codex_exec


def case_insensitive(folder: Path) -> bool:
    probe = folder / "CaseProbe"
    probe.mkdir(exist_ok=True)
    try:
        return (folder / "caseprobe").exists()
    finally:
        probe.rmdir()


@pytest.fixture
def ci(tmp_path):
    if not case_insensitive(tmp_path):
        pytest.skip("the temp filesystem is case-sensitive")
    return tmp_path


def _variant(path: Path) -> Path:
    return path.parent / path.name.swapcase()


# --- everywhere -------------------------------------------------------------------------------


def test_canonical_resolves_and_keeps_a_missing_tail(tmp_path):
    (tmp_path / "a").mkdir()
    assert pathid.canonical(tmp_path / "a" / ".." / "a") == (tmp_path / "a").resolve()
    assert pathid.canonical(tmp_path / "a" / "Not" / "Yet") == (tmp_path / "a").resolve() / "Not" / "Yet"


def test_inside_same_and_overlap(tmp_path):
    inner = tmp_path / "a" / "b"
    inner.mkdir(parents=True)
    assert pathid.inside(inner, tmp_path / "a") and pathid.inside(tmp_path / "a", tmp_path / "a")
    assert not pathid.inside(tmp_path / "a", inner)
    assert pathid.overlap(tmp_path / "a", inner) and pathid.overlap(inner, tmp_path / "a")
    assert not pathid.overlap(inner, tmp_path / "c")
    assert pathid.same(tmp_path / "a", tmp_path / "a" / "b" / "..")
    assert pathid.top_component(inner, tmp_path) == "a"
    assert pathid.top_component(tmp_path, tmp_path) is None
    assert pathid.top_component(tmp_path / "c", tmp_path / "a") is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_identity_holds_when_the_spelling_differs(tmp_path, monkeypatch):
    """Two spellings of one folder (as a firmlink or a case variant gives) compare by identity."""
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    # A canonicaliser that cannot see through the alias: only identity is left.
    monkeypatch.setattr(pathid, "canonical", lambda p: Path(os.path.abspath(p)))
    assert pathid.inside(alias / "sub", real)
    assert pathid.inside(real / "sub", alias)
    assert pathid.same(alias, real)
    assert pathid.top_component(alias / "sub", real) == "sub"
    assert not pathid.inside(tmp_path, alias)


# --- case-insensitive filesystems -------------------------------------------------------------


def test_canonical_spells_a_case_variant_as_stored(ci):
    (ci / "Library" / "Application Support").mkdir(parents=True)
    assert pathid.canonical(ci / "LIBRARY" / "application support") == (
        ci.resolve() / "Library" / "Application Support")
    assert pathid.canonical(ci / "library" / "Missing") == ci.resolve() / "Library" / "Missing"
    assert pathid.inside(ci / "LIBRARY" / "application support", ci / "Library")
    assert pathid.same(ci / "library", ci / "Library")


def test_the_worker_directory_in_another_case_is_never_granted(ci):
    root = ci / "backup"
    (root / "worker" / "research").mkdir(parents=True)
    (root / "worker" / "leases").mkdir()
    allowed = codex_exec.granted_root_allowed
    assert not allowed(root, ci / "BACKUP")
    assert not allowed(root, ci / "Backup" / "WORKER")
    assert not allowed(root, ci / "backup" / "Worker" / "LEASES")
    assert allowed(root, ci / "BACKUP" / "worker" / "RESEARCH")


def test_settings_refuse_and_store_case_variants(ci):
    root = ci / "backup"
    root.mkdir(mode=0o700)
    out = ci / "Results"
    out.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="disjoint"):
        set_worker_workspaces(root, (WorkerWorkspace("a", out, (ci / "RESULTS" / "..",)),))
    with pytest.raises(ValueError, match="disjoint"):
        set_worker_workspaces(root, (WorkerWorkspace("a", out / "job", (ci / "results",)),))
    (ci / "Code").mkdir()
    set_worker_workspaces(root, (WorkerWorkspace("a", ci / "results", (ci / "CODE",)),))
    (workspace,) = load_worker_settings(root).workspaces
    assert workspace.output_root == ci.resolve() / "Results"
    assert workspace.readonly_roots == (ci.resolve() / "Code",)
