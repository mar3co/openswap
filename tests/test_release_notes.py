"""The GitHub release body comes from CHANGELOG.md."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "release_notes.py"
CHANGELOG = """# Changelog

<!--
## 9.9.9 (2026-01-01)
- not a real section
-->

## Unreleased

### New

- Something coming.

## 1.2.0 (2026-10-17)

The intro.

### Fixed

- A fix in Settings → General.

## 1.1.0 (2026-09-07)

## 1.0.0

- First.
"""


def _notes(version: str, path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), version, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_prints_only_that_versions_section(tmp_path: Path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    result = _notes("1.2.0", path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "The intro.\n\n### Fixed\n\n- A fix in Settings → General.\n"


def test_a_heading_without_a_date_still_matches(tmp_path: Path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    assert _notes("1.0.0", path).stdout == "- First.\n"


def test_missing_empty_and_commented_sections_fail(tmp_path: Path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(CHANGELOG, encoding="utf-8")
    for version in ("2.0.0", "1.1.0", "9.9.9", "1.2"):
        result = _notes(version, path)
        assert result.returncode == 1, version
        assert "## Unreleased" in result.stderr


def test_the_current_version_has_notes():
    # A version bump has to rename Unreleased in the same PR, or the tag's
    # release job fails after both architectures have already built.
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    result = _notes(version, ROOT / "CHANGELOG.md")
    assert result.returncode == 0, result.stderr
