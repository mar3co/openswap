# Plan 006: Refuse PyPI upgrade for an editable git checkout

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, do **not** update `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 5e4ffde..HEAD -- src/openswap/update_check.py tests/test_update_check.py README.md pyproject.toml docs/hacking.md`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `5e4ffde`, 2026-09-05

## Why this matters

This fork’s extra, widget, and kickoff are not on PyPI. `openswap upgrade` runs
`uv tool upgrade openswap`, which installs `realiti4`’s wheel and drops
those features. `check_for_update` nags about PyPI even for an editable
checkout. After this plan, an install whose `openswap` package lives inside
a git work tree refuses PyPI upgrade and does not nag; README/pyproject point
at the mar3co repo.

## Current state

- `src/openswap/update_check.py`
  - `PYPI_URL = "https://pypi.org/pypi/openswap/json"`
  - `check_for_update` compares PyPI latest to `current_version`
  - `run_self_upgrade` runs `uv tool upgrade openswap` or `pipx upgrade openswap`
  - Comment already: `If you installed with pip install -e ., use git pull instead.`
- `pyproject.toml` `[project.urls]` still:
  `Homepage` / `Repository` / `Issues` → `https://github.com/realiti4/claude-swap`
- `README.md` first install is `uv tool install 'openswap[menubar]'` (PyPI),
  fork clone is secondary.
- `docs/hacking.md` already says PyPI will not see this checkout’s widget
  sources.

This machine: `uv tool install --editable '.[menubar]'` so
`openswap.__file__` is under the git checkout, which has
`.git`. Detection must use the **package file path**, not `sys.prefix`
(prefix is still `uv/tools`).

Do not rename the PyPI package. Do not publish.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests | `uv run pytest tests/test_update_check.py -n auto` | all pass |

## Suggested executor toolkit

- TDD: failing tests first.
- Never print or commit secrets. None expected here.

## Scope

**In scope**:
- `src/openswap/update_check.py`
- `tests/test_update_check.py`
- `README.md`
- `pyproject.toml` (`[project.urls]` only)
- `docs/hacking.md` (one sentence if upgrade is not mentioned)

**Out of scope**:
- Changing `name = "openswap"` or version
- A new PyPI project
- Widget / extra code
- Push / `upstream`

## Git workflow

- Branch: `advisor/006-refuse-pypi-upgrade`
- Commit: `fix(upgrade): do not replace a git checkout with PyPI`
- Do NOT push.

## Steps

### Step 1: Drift check

**Verify**: in-scope files match excerpts, or STOP.

### Step 2: Failing tests (TDD)

Add to `tests/test_update_check.py` (model after `TestCheckForUpdate`):

```python
def test_package_from_git_checkout_is_detected(tmp_path, monkeypatch):
    # tmp_path is a fake clone: .git dir + src/openswap/__init__.py
    # _package_is_git_checkout(path) is True

def test_package_inside_uv_tools_without_git_is_not_a_checkout(tmp_path):
    # path under uv/tools/openswap, no .git ancestor → False

def test_check_for_update_skips_pypi_for_git_checkout(monkeypatch, tmp_path):
    # patch detection True; urlopen must NOT be called; result is None

def test_run_self_upgrade_refuses_pypi_for_git_checkout(monkeypatch, capsys):
    # patch detection True; subprocess.run must NOT be called
    # return code is 1
    # stdout/stderr mentions git pull and uv tool install --editable
```

Do **not** hit the real network. Do **not** inspect the real user checkout
except via injected paths.

**Verify**: tests fail because `_package_is_git_checkout` / wiring is missing.

### Step 3: Implement detection + guards

In `update_check.py`:

```python
def _package_is_git_checkout(package_file: Path | None = None) -> bool:
    """True when openswap is loaded from a directory that has a .git ancestor.

    Walk parents of package_file (default: openswap.__file__). Stop at
    filesystem root. A `.git` file (gitdir for worktrees) or directory both
    count. Never follow the path into uv/tools or pipx as a positive: those
    copies have no `.git`.
    """
```

- `check_for_update`: if `_package_is_git_checkout()`, return `None` before
  cache/network.
- `run_self_upgrade`: if `_package_is_git_checkout()`, print a short message
  using existing `printer.error` / print:
  this is a git checkout; PyPI would drop fork features; upgrade with
  `git pull` then `uv tool install --editable '.[menubar]'`.
  Return `1`. Do not run `uv tool upgrade`.

Keep uv/pipx upgrade for non-checkout installs.

**Verify**: `uv run pytest tests/test_update_check.py -n auto` passes,
including existing PyPI tests.

### Step 4: README + pyproject URLs

`pyproject.toml` `[project.urls]`:

```toml
Homepage = "https://github.com/mar3co/openswap"
Repository = "https://github.com/mar3co/openswap"
Issues = "https://github.com/mar3co/openswap/issues"
```

`README.md` Install section: put the **fork clone + editable** block first.
Keep `uv tool install 'openswap[menubar]'` as “upstream PyPI (no extra
widget/kickoff from this fork)”. Mention that `openswap upgrade` on an editable
checkout will refuse PyPI.

`docs/hacking.md`: one sentence that `openswap upgrade` refuses PyPI when running
from this tree.

**Verify**: grep `realiti4/openswap` in `pyproject.toml` has no matches
under `[project.urls]`. README still mentions upstream once as attribution
(that is fine).

### Step 5: Commit

**Verify**: `git diff --stat` only in-scope files.

## Test plan

- git checkout detection true/false
- check_for_update short-circuit
- run_self_upgrade does not subprocess
- existing TestCheckForUpdate cases still pass
- Pattern: `tests/test_update_check.py`

## Done criteria

- [ ] `uv run pytest tests/test_update_check.py -n auto` exits 0
- [ ] Editable git install: no PyPI nag, no `uv tool upgrade openswap`
- [ ] `[project.urls]` point at mar3co
- [ ] README leads with the fork clone
- [ ] No files outside scope
- [ ] One conventional commit on `advisor/006-refuse-pypi-upgrade`

## STOP conditions

- Drift mismatch.
- You think you need to change the package name or publish to PyPI.
- Detection would require running `git` as a subprocess against the user’s
  real repo (use path walking only).
- Existing PyPI tests break and cannot be fixed without weakening them.

## Maintenance notes

- If this fork is ever published under a different name, replace the guard
  with that name rather than deleting it.
- Reviewer: confirm `uv tool install --editable` is detected (package file in
  the clone, `.git` ancestor), not `sys.prefix`.
