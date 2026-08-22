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
from ruamel.yaml import YAML

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


def test_verify_gates_announces_degraded_identity_mode(tmp_path: Path) -> None:
    """A fork runs the PII gate with half its patterns; say so rather than imply a full scan."""
    result = _run(_sandbox(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEGRADED" in result.stdout, (
        f"no degraded-mode notice with no identity file:\n{result.stdout}"
    )


def test_verify_gates_is_quiet_when_identity_patterns_load(tmp_path: Path) -> None:
    """Control for the case above: with an identity file there is nothing to warn about."""
    root = _sandbox(tmp_path)
    identity = root / "identity.json"
    identity.write_text('{"github_username": "someone"}\n', encoding="utf-8")

    result = subprocess.run(  # noqa: S603 - test-only, controlled args
        [BASH, str(root / ".githooks" / "verify-gates.sh")],
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(root),
            "PII_IDENTITY_FILE": str(identity),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DEGRADED" not in result.stdout


def _run_script(root: Path, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test-only, controlled args
        [BASH, str(root / ".githooks" / script), *args],
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


def test_revendor_reproduces_the_committed_lockfile(tmp_path: Path) -> None:
    """The committed vendor.lock is what the tool produces, not a hand-written file.

    Re-vendoring from the current vendored copies is a no-op by construction, so
    any difference means the lockfile and the tool disagree about the vendored
    set or the line format.
    """
    root = _sandbox(tmp_path)
    (root / ".githooks" / "vendor.lock").write_text("# emptied\n", encoding="utf-8")

    result = _run_script(root, "revendor.sh", "--source", str(GITHOOKS))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / ".githooks" / "vendor.lock").read_text() == (
        GITHOOKS / "vendor.lock"
    ).read_text()


def test_revendor_refuses_an_incomplete_source(tmp_path: Path) -> None:
    """Half a re-vendor leaves a tree whose hash check fails for no obvious reason."""
    root = _sandbox(tmp_path)
    partial = tmp_path / "partial"
    partial.mkdir()
    shutil.copy(GITHOOKS / "pii-scan.sh", partial / "pii-scan.sh")
    before = (root / ".githooks" / "pii-patterns.conf").read_text()

    result = _run_script(root, "revendor.sh", "--source", str(partial))

    assert result.returncode == 2
    assert "source is missing" in result.stderr
    assert (root / ".githooks" / "pii-patterns.conf").read_text() == before


def test_revendor_refuses_a_source_that_does_not_exist(tmp_path: Path) -> None:
    result = _run_script(_sandbox(tmp_path), "revendor.sh", "--source", str(tmp_path / "nope"))

    assert result.returncode == 2
    assert "does not exist" in result.stderr


def test_githooks_readme_instructions_are_followable_by_anyone() -> None:
    """The re-vendor instructions used to be `cp ~/.claude/scripts/...`.

    No contributor has that directory, so the documented way to take an upstream
    change was a command only one machine could run.
    """
    readme = (GITHOOKS / "README.md").read_text(encoding="utf-8")

    assert "~/.claude" not in readme, "README instructs a copy from a machine-only path"
    assert "revendor.sh" in readme, "README does not name the re-vendor entry point"


def test_verify_gates_survives_a_machine_without_timeout(tmp_path: Path) -> None:
    """Stock macOS has no GNU ``timeout``; calling it anyway returned rc=127.

    Every gate call then looked like a broken gate, so the verifier reported
    FAIL four times over on a machine whose gates were fine - the exact false
    alarm the timeout wrapper exists to prevent.
    """
    root = _sandbox(tmp_path)
    result = subprocess.run(  # noqa: S603 - test-only, controlled args
        [BASH, str(root / ".githooks" / "verify-gates.sh")],
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": "/usr/bin:/bin",  # no coreutils, as on a GitHub macOS runner
            "HOME": str(root),
            "PII_IDENTITY_FILE": str(root / "no-such-identity.json"),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no timeout(1) on PATH" in result.stdout, (
        f"ran without a hang guard and did not say so:\n{result.stdout}"
    )


def _gates_pii_step() -> str:
    """The gates job's PII step, as CI runs it."""
    workflow = YAML(typ="safe").load(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    for step in workflow["jobs"]["gates"]["steps"]:
        if "PII scan" in str(step.get("name", "")):
            script: str = step["run"]
            return script
    pytest.fail("the gates job has no PII scan step")


def _scratch_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / ".githooks").mkdir(parents=True)
    for name in ("pii-scan.sh", "pii-patterns.conf"):
        shutil.copy(GITHOOKS / name, repo / ".githooks" / name)
    subprocess.run([GIT, "init", "-q", "--initial-branch=main", "."], cwd=repo, check=True)  # noqa: S603
    for key, value in (
        ("user.email", "engram-test@example.com"),
        ("user.name", "engram-test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run([GIT, "config", key, value], cwd=repo, check=True)  # noqa: S603
    return repo


def _commit(repo: Path, name: str, body: str) -> None:
    (repo / name).write_text(body, encoding="utf-8")
    subprocess.run([GIT, "add", "-A"], cwd=repo, check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [GIT, "commit", "-q", "--no-verify", "-m", f"add {name}"], cwd=repo, check=True
    )


def _run_gates_step(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test-only, controlled args
        [BASH, "-e", "-c", _gates_pii_step()],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(repo),
            "BASE_SHA": base,
            "PII_IDENTITY_FILE": str(repo / "no-such-identity.json"),
        },
    )


def test_ci_pii_step_flags_pii_a_ref_introduces(tmp_path: Path) -> None:
    """Runs the workflow's own step, because nothing else executes that shell.

    It carried ``${#FILES[@]:-0}`` for its whole life: bash 3.2 accepts that,
    bash 5 calls it a bad substitution, so every local run looked clean while
    the step died on the runner before scanning anything. ``bash -n`` cannot
    see it either - the expansion has to actually run.

    Reach: this fails only where the default ``bash`` is 4+. On macOS (bash
    3.2) it passes either way, which is exactly how the bug survived; the
    Linux CI job is where it has teeth.
    """
    repo = _scratch_repo(tmp_path)
    _commit(repo, "clean.md", "nothing sensitive here\n")
    base = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    planted = "see /Users/someone/secret for details\n"  # pii-allow: planted probe fixture
    _commit(repo, "leaked.md", planted)

    result = _run_gates_step(repo, base)

    assert result.returncode == 1, (
        f"planted PII walked past the CI step:\n{result.stdout}\n{result.stderr}"
    )
    assert "leaked.md" in result.stdout


def test_ci_pii_step_passes_a_clean_ref(tmp_path: Path) -> None:
    """The control: the step must not fail a ref that introduces nothing bad."""
    repo = _scratch_repo(tmp_path)
    _commit(repo, "clean.md", "nothing sensitive here\n")
    base = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _commit(repo, "also-clean.md", "still nothing sensitive\n")

    result = _run_gates_step(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "scanning 1 changed file(s)" in result.stdout
