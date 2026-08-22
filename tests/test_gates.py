"""Tests for the vendored repo gates in ``.githooks/``.

The gates guard content safety, so they need the same regression pressure as
the code they protect. Two properties matter and neither implies the other:

* the vendored scripts are unmodified (``vendor.lock`` hashes), and
* the gates still *detect* what they exist to detect.

A scanner replaced by ``exit 0`` and re-hashed honestly satisfies the first
and fails the second, which is why every case below asserts on behaviour
rather than only on the lockfile.

Each test runs against a copy of ``.githooks/`` in ``tmp_path`` so a mutation
never touches the real tree. ``PII_IDENTITY_FILE`` is pointed at a path that
does not exist, so results do not depend on whether the machine running the
suite happens to have a personal identity file.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash") or "bash"
GIT = shutil.which("git") or "git"
SHASUM = shutil.which("shasum") or "shasum"

REPO_ROOT = Path(__file__).resolve().parent.parent
GITHOOKS = REPO_ROOT / ".githooks"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not GITHOOKS.is_dir(),
    reason="requires bash and a .githooks directory",
)


def _sandbox(tmp_path: Path) -> Path:
    """Copy .githooks into tmp_path; verify-gates resolves its root from there."""
    shutil.copytree(GITHOOKS, tmp_path / ".githooks")
    return tmp_path


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test-only, controlled args
        [BASH, str(root / ".githooks" / "verify-gates.sh")],
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(root),
            "PII_IDENTITY_FILE": str(root / "no-such-identity.json"),
        },
    )


def _relock(root: Path) -> None:
    """Regenerate vendor.lock so a mutation is hash-honest."""
    lock = root / ".githooks" / "vendor.lock"
    names = [
        line.split()[1]
        for line in lock.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    out = ["# sha256  path"]
    for name in names:
        digest = subprocess.run(  # noqa: S603 - test-only, controlled args
            [SHASUM, "-a", "256", str(root / name)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()[0]
        out.append(f"{digest}  {name}  # pii-allow: sha256")
    lock.write_text("\n".join(out) + "\n")


def test_passes_on_an_unmodified_checkout(tmp_path: Path) -> None:
    result = _run(_sandbox(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_detects_a_hand_edited_vendored_file(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    scanner = root / ".githooks" / "pii-scan.sh"
    scanner.write_text(scanner.read_text() + "\n# local edit\n")

    result = _run(root)
    assert result.returncode == 1
    assert "hash drift" in result.stderr


def test_refuses_a_lockfile_that_pins_nothing(tmp_path: Path) -> None:
    root = _sandbox(tmp_path)
    (root / ".githooks" / "vendor.lock").write_text("# every entry removed\n")

    result = _run(root)
    assert result.returncode == 1
    assert "zero entries" in result.stderr


def test_detects_a_scanner_that_stopped_detecting(tmp_path: Path) -> None:
    """The case a hash check cannot catch: a broken gate, re-hashed honestly."""
    root = _sandbox(tmp_path)
    (root / ".githooks" / "pii-scan.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    _relock(root)

    result = _run(root)
    assert result.returncode == 1
    assert "did NOT flag planted PII" in result.stderr
    assert "hash drift" not in result.stderr


def test_the_whole_tracked_tree_passes_the_pii_scan() -> None:
    """No tracked file carries structural PII, with no identity file present.

    The CI gate scans only the files a ref changes, which cannot see what the
    tree already carried; this scans everything. ``LICENSE`` is excluded: its
    copyright attribution is the one sanctioned place for the maintainer's
    name, and a license text must not carry a scanner marker.

    ``PII_IDENTITY_FILE`` points at nothing, so this measures what a fork or a
    CI runner measures - structural patterns only. The identity half (name,
    emails, username) is machine-local by construction and stays with the
    maintainer's pre-commit hook.
    """
    tracked = subprocess.run(  # noqa: S603 - test-only, controlled args
        [GIT, "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    files = [f for f in tracked if f != "LICENSE"]
    assert files, "git ls-files returned nothing; the scan would be a no-op"

    result = subprocess.run(  # noqa: S603 - test-only, controlled args
        [BASH, str(GITHOOKS / "pii-scan.sh"), *files],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(REPO_ROOT),
            "PII_IDENTITY_FILE": str(REPO_ROOT / "no-such-identity.json"),
        },
    )
    assert result.returncode == 0, (
        "tracked files carry PII the changed-files CI scan cannot see:\n" + result.stdout
    )
