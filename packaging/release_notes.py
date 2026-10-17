"""Print one release's section of CHANGELOG.md, for the GitHub release body.

    python3 packaging/release_notes.py 0.2.0 [CHANGELOG.md]

The section is everything under "## 0.2.0 (date)" up to the next "## "
heading, without the heading itself. It fails when the version has no
section or the section is empty, so a tag cannot ship without notes. HTML
comments are skipped, so the example heading in the file's own comment never
matches.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def section(text: str, version: str) -> str | None:
    heading = re.compile(rf"^## {re.escape(version)}(?: \(.*\))?\s*$")
    lines = COMMENT.sub("", text).splitlines()
    for start, line in enumerate(lines):
        if heading.match(line):
            body: list[str] = []
            for line in lines[start + 1 :]:
                if line.startswith("## "):
                    break
                body.append(line)
            notes = "\n".join(body).strip()
            return notes or None
    return None


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: release_notes.py <version> [CHANGELOG.md]", file=sys.stderr)
        return 2
    version = argv[1]
    path = Path(argv[2]) if len(argv) == 3 else ROOT / "CHANGELOG.md"
    notes = section(path.read_text(encoding="utf-8"), version)
    if notes is None:
        print(
            f"release_notes.py: {path.name} has no notes for {version}. "
            f'Rename "## Unreleased" to "## {version} (YYYY-MM-DD)" first.',
            file=sys.stderr,
        )
        return 1
    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
