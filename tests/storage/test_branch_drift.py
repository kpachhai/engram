"""Tests for VaultStorage.current_branch_drifted + check_branch_drift doctor probe.

Branch-drift is a monitor-and-warn surface (the storage layer cannot
prevent a side-channel ``git checkout``). The mount-time branch is
captured in __init__; subsequent checks compare against the current
branch HEAD.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from engram.diagnostics.check_codes import GIT_BRANCH_DRIFTED
from engram.diagnostics.phase4_checks import check_branch_drift
from engram.storage.facade import VaultStorage


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    _git(["config", "commit.gpgsign", "false"], cwd=path)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=path)
    _git(["commit", "-m", "seed"], cwd=path)


def test_branch_drift_returns_false_for_non_git_dir(tmp_path: Path) -> None:
    """Storage mounted under a non-git dir returns drifted=False."""
    storage = VaultStorage(
        thoughts_dir=tmp_path / "thoughts",
        index_db_path=tmp_path / ".indexes" / "engram.db",
    )
    drifted, mounted_at, current = storage.current_branch_drifted()
    assert drifted is False
    assert mounted_at is None
    assert current is None
    storage.close()


def test_branch_drift_detects_checkout(tmp_path: Path) -> None:
    """A git checkout after mount surfaces drift on the next probe."""
    repo = tmp_path / "vault"
    _init_git_repo(repo)
    storage = VaultStorage(
        thoughts_dir=repo / "thoughts",
        index_db_path=repo / ".indexes" / "engram.db",
    )
    # Snapshot the mount-time branch.
    drifted, mounted_at, current = storage.current_branch_drifted()
    assert drifted is False
    assert mounted_at == "main"
    assert current == "main"
    # Side-channel checkout to a new branch.
    _git(["checkout", "-b", "feature-x"], cwd=repo)
    drifted, mounted_at, current = storage.current_branch_drifted()
    assert drifted is True
    assert mounted_at == "main"
    assert current == "feature-x"
    storage.close()


def test_check_branch_drift_returns_warn_row() -> None:
    """The doctor probe surfaces drift as a WARN row with both branches."""
    storage = MagicMock()
    storage.current_branch_drifted.return_value = (True, "main", "feature-x")
    rows = check_branch_drift(storages={"team-x": storage})
    assert len(rows) == 1
    assert rows[0].code == GIT_BRANCH_DRIFTED
    assert rows[0].status == "WARN"
    assert "main" in rows[0].detail
    assert "feature-x" in rows[0].detail


def test_check_branch_drift_skips_non_drifted() -> None:
    storage = MagicMock()
    storage.current_branch_drifted.return_value = (False, "main", "main")
    rows = check_branch_drift(storages={"team-x": storage})
    assert rows == []


def test_check_branch_drift_skips_storages_without_method() -> None:
    """Backwards-compat: storages lacking the method are skipped silently."""
    storage = MagicMock(spec=[])  # no methods
    rows = check_branch_drift(storages={"x": storage})
    assert rows == []
