"""Tests for engram.sync.startup_probes (one positive + one negative per probe)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engram.config.models import SyncConfig
from engram.diagnostics import check_codes
from engram.sync import startup_probes
from engram.sync.identity import IDENTITY_FILE_RELATIVE
from engram.sync.startup_probes import (
    ProbeReport,
    run_startup_probes,
)

from .conftest import init_repo, run_git


def _seeded_repo(tmp_path: Path, *, gitignore_ok: bool = True) -> Path:
    repo = tmp_path / "vault"
    init_repo(repo, bare=False)
    if gitignore_ok:
        (repo / ".gitignore").write_text(".indexes/\n*.sqlite\n*.sqlite-wal\n*.sqlite-shm\n")
    (repo / "thoughts").mkdir()
    (repo / "thoughts" / "seed.md").write_text("seed")
    cp_add = run_git(["add", "."], repo)
    assert cp_add.returncode == 0, cp_add.stderr
    cp_commit = run_git(["commit", "-m", "seed"], repo)
    assert cp_commit.returncode == 0, cp_commit.stderr
    return repo


# === gitignore probe ===


def test_probe_gitignore_missing_fails(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    report = ProbeReport()
    startup_probes.probe_gitignore_indexes(tmp_path, report)
    codes = [f.code for f in report.failures]
    assert check_codes.GITIGNORE_INDEXES in codes


def test_probe_gitignore_full_passes(tmp_path: Path) -> None:
    repo = _seeded_repo(tmp_path)
    report = ProbeReport()
    startup_probes.probe_gitignore_indexes(repo, report)
    assert not report.has_failures


def test_probe_gitignore_partial_fails(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    (tmp_path / ".gitignore").write_text(".indexes/\n")  # missing *.sqlite
    report = ProbeReport()
    startup_probes.probe_gitignore_indexes(tmp_path, report)
    assert any(f.code == check_codes.GITIGNORE_INDEXES for f in report.failures)


# === cloud sync ===


def test_probe_cloud_sync_clean_path(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    report = ProbeReport()
    startup_probes.probe_cloud_sync(tmp_path, report)
    assert not report.has_failures


def test_probe_cloud_sync_under_dropbox_path() -> None:
    """If the resolved path contains a known cloud-sync hint, FAIL.

    Constructed via a synthetic path that doesn't actually have to exist;
    the check operates on path components, not file presence.
    """
    fake = Path("/Users/x/Dropbox/vault")  # pii-allow: synthetic test path
    report = ProbeReport()
    # Monkey-call requires the path to exist for git_dir.resolve(); construct
    # a temporary symlink in /tmp and verify the path-component check directly.
    parts_lower = [p.lower() for p in fake.parts]
    assert any(hint.lower() in parts_lower for hint in startup_probes._CLOUD_SYNC_HINTS)
    # Now exercise the function with an existing but synthetic path.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="Dropbox_") as td:
        fake_dir = Path(td) / "Dropbox" / "vault"
        fake_dir.mkdir(parents=True)
        startup_probes.probe_cloud_sync(fake_dir, report)
    assert any(f.code == check_codes.CLOUD_SYNC_UNDER_DOTGIT for f in report.failures)


# === read-only role contradiction ===


def test_probe_read_only_consistent_passes() -> None:
    config = SyncConfig(role="read-only", auto_push_on_capture=False)
    report = ProbeReport()
    startup_probes.probe_read_only_role_consistency(config, report)
    assert not report.has_failures


def test_probe_read_only_with_auto_push_fails() -> None:
    config = SyncConfig(role="read-only", auto_push_on_capture=True)
    report = ProbeReport()
    startup_probes.probe_read_only_role_consistency(config, report)
    assert any(f.code == check_codes.READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH for f in report.failures)


# === signed commits ===


def test_probe_signed_commits_off_is_silent() -> None:
    config = SyncConfig(signed_pull_required=False)
    report = ProbeReport()
    startup_probes.probe_signed_commits_required(config, report)
    assert not report.warnings


def test_probe_signed_commits_required_without_keys_warns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = SyncConfig(signed_pull_required=True)
    report = ProbeReport()
    startup_probes.probe_signed_commits_required(config, report)
    assert any(w.code == check_codes.SIGNED_COMMITS_REQUIRED for w in report.warnings)


# === branch + working tree (real git) ===


def test_probe_branch_alignment_default_branch_passes(tmp_path: Path) -> None:
    repo = _seeded_repo(tmp_path)
    config = SyncConfig()
    report = asyncio.run(run_startup_probes(config, repo))
    fail_codes = [f.code for f in report.failures]
    # The fresh seeded repo has no remote; that produces only WARN-level messages.
    assert check_codes.BRANCH_ALIGNMENT not in fail_codes
    # No working-tree-dirty failure (everything committed).
    assert check_codes.WORKING_TREE_DIRTY_AT_STARTUP not in fail_codes


def test_probe_dirty_tree_fails(tmp_path: Path) -> None:
    repo = _seeded_repo(tmp_path)
    (repo / "uncommitted.md").write_text("dirty")
    report = ProbeReport()
    asyncio.run(startup_probes.probe_working_tree_dirty(repo, report))
    assert any(f.code == check_codes.WORKING_TREE_DIRTY_AT_STARTUP for f in report.failures)


def test_probe_user_identity_unset_warns(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    # init_repo set user.email/user.name; clear them.
    run_git(["config", "--unset", "user.email"], tmp_path)
    run_git(["config", "--unset", "user.name"], tmp_path)
    report = ProbeReport()
    asyncio.run(startup_probes.probe_user_identity(tmp_path, report))
    assert any(w.code == check_codes.SYNC_USER_IDENTITY_SET for w in report.warnings)


def test_probe_user_identity_set_passes(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    report = ProbeReport()
    asyncio.run(startup_probes.probe_user_identity(tmp_path, report))
    assert not any(w.code == check_codes.SYNC_USER_IDENTITY_SET for w in report.warnings)


# === identity check ===


def test_probe_vault_identity_no_remote_skips(tmp_path: Path) -> None:
    # No push path = no contamination risk; check is skipped for remote-less vaults.
    repo = _seeded_repo(tmp_path)
    pattern = "^/.+/seed-test/.*$"
    (repo / IDENTITY_FILE_RELATIVE).parent.mkdir(parents=True, exist_ok=True)
    (repo / IDENTITY_FILE_RELATIVE).write_text(
        f"vault_id: testvault\nexpected_remote_pattern: '{pattern}'\n"
    )
    config = SyncConfig()
    report = asyncio.run(run_startup_probes(config, repo))
    all_codes = [f.code for f in report.failures] + [w.code for w in report.warnings]
    assert check_codes.VAULT_IDENTITY_REMOTE_MATCH not in all_codes


def test_probe_vault_identity_missing_no_remote_skips(tmp_path: Path) -> None:
    # No identity.local + no remote: contamination check is skipped (no WARN).
    repo = _seeded_repo(tmp_path)
    config = SyncConfig()
    report = asyncio.run(run_startup_probes(config, repo))
    warn_codes = [w.code for w in report.warnings]
    assert check_codes.VAULT_IDENTITY_REMOTE_MATCH not in warn_codes


# === git version ===


def test_probe_git_version_meets_floor(tmp_path: Path) -> None:
    init_repo(tmp_path, bare=False)
    report = ProbeReport()
    asyncio.run(startup_probes.probe_git_version(tmp_path, report))
    assert not report.has_failures


# === aggregate run ===


def test_run_startup_probes_disabled_returns_empty(tmp_path: Path) -> None:
    repo = _seeded_repo(tmp_path)
    config = SyncConfig(disabled=True)
    report = asyncio.run(run_startup_probes(config, repo))
    assert not report.failures
    assert not report.warnings


def test_run_startup_probes_aggregates_all_probes(tmp_path: Path) -> None:
    """At least one probe produces a result (aggregate wiring check)."""
    repo = _seeded_repo(tmp_path)
    # Clear per-vault git identity so SYNC_USER_IDENTITY_SET fires.
    run_git(["config", "--unset", "user.email"], repo)
    run_git(["config", "--unset", "user.name"], repo)
    config = SyncConfig()
    report = asyncio.run(run_startup_probes(config, repo))
    warn_codes = [w.code for w in report.warnings]
    assert check_codes.SYNC_USER_IDENTITY_SET in warn_codes


def test_per_cycle_recheck_skips_when_disabled(tmp_path: Path) -> None:
    repo = _seeded_repo(tmp_path)
    config = SyncConfig(disabled=True)
    report = asyncio.run(startup_probes.per_cycle_recheck(config, repo))
    assert not report.failures
    assert not report.warnings


def test_serialize_failures_renders_lines(tmp_path: Path) -> None:
    from engram.sync.startup_probes import ProbeFailure, serialize_failures

    failures = [
        ProbeFailure(code="x", message="a", detail="d"),
        ProbeFailure(code="y", message="b"),
    ]
    out = serialize_failures(failures)
    assert "[x] a" in out
    assert "d" in out
    assert "[y] b" in out
