"""Every path a reader is pointed at must exist in a clone of this repo.

Docs used to cite the spec at `docs/superpowers/specs/...` and the retrospectives
at `~/repos/github.com/<maintainer>/...`. Neither is in the repo - the first is
gitignored, the second lives on one machine - so both read as "go look at this"
while being unopenable for everyone but the maintainer.

Two exemptions, both about not rewriting a record after the fact:

* `docs/archive/` holds delivery plans as they were written.
* `CHANGELOG.md` records what happened per release.

Placeholder paths (`<your-username>`, `<your-meta-stack-repo>`) are fine: they
are visibly a blank for the reader to fill, not a claim that a file is there.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SELF = str(Path(__file__).resolve().relative_to(REPO_ROOT))

#: Records of the past, not directions to a reader.
_EXEMPT_PREFIXES = ("docs/archive/", "CHANGELOG.md")

#: Files that configure tooling around the gitignored spec directory rather
#: than pointing a reader at it.
_EXEMPT_EXACT = (".gitignore", "pyproject.toml")

#: Paths that only resolve on the maintainer's machine. `~/repos/...` is only a
#: dead pointer when it names a real directory; with a `<placeholder>` segment
#: it is a fill-in-the-blank.
_UNREACHABLE = (
    re.compile(r"docs/superpowers/"),
    re.compile(r"~/repos/(?![^\s`)]*<)"),
)


def _tracked_text_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],  # noqa: S607 - resolved from PATH by design
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    keep: list[str] = []
    for path in out:
        if path.startswith(_EXEMPT_PREFIXES) or path in _EXEMPT_EXACT:
            continue
        if path.startswith(".githooks/"):
            continue  # vendored gate scripts; their allowlists name the directory
        if path == _SELF:
            continue  # this file spells out the patterns it looks for
        if path.endswith((".md", ".py", ".yaml", ".yml", ".toml")):
            keep.append(path)
    return keep


def test_no_tracked_file_points_at_an_unreachable_path() -> None:
    tracked = _tracked_text_files()
    assert tracked, "git ls-files returned nothing; this test would prove nothing"

    offenders: list[str] = []
    for rel in tracked:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in _UNREACHABLE):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, "references a reader cannot open:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("see docs/superpowers/specs/x.md", True),
        ("cd ~/repos/github.com/someone/engram", True),
        ("cd ~/repos/github.com/<your-username>/engram", False),
        ("`<your-meta-stack-repo>/workspace/engram/NOTES.md`", False),
        ("see docs/adr/001-storage-recipe.md", False),
    ],
)
def test_the_unreachable_patterns_discriminate(line: str, expected: bool) -> None:
    """Control for the test above: a matcher that flags everything proves nothing."""
    assert any(pattern.search(line) for pattern in _UNREACHABLE) is expected
