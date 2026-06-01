"""Step 13 - 14 doctor sync checks (positive + negative each)."""

from __future__ import annotations

from pathlib import Path

import pytest

from engram.config.models import (
    DEFAULT_EMBEDDING_MODEL,
    EffectiveConfig,
    LLMConfig,
    SyncConfig,
)
from engram.diagnostics import check_codes
from engram.diagnostics.doctor import (
    CheckStatus,
    DoctorReport,
    run_diagnostics,
    run_sync_diagnostics,
)
from engram.storage.facade import VaultStorage
from tests.sync.conftest import init_repo, run_git


def _seeded_git_vault(
    tmp_path: Path,
    *,
    gitignore_ok: bool = True,
) -> Path:
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


def _config_for(repo: Path) -> EffectiveConfig:
    indexes = repo / ".indexes"
    indexes.mkdir(exist_ok=True)
    return EffectiveConfig(
        default_user="test",
        vault_path=repo,
        thoughts_dir=repo / "thoughts",
        index_dir=indexes,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        vault_name="default",
        sync=SyncConfig(),
        llm=LLMConfig(),
    )


# === non-git vault ===


def test_non_git_vault_skips_all_sync_checks(tmp_path: Path) -> None:
    """A vault without .git emits OK rows for every sync code."""
    thoughts = tmp_path / "thoughts"
    thoughts.mkdir()
    indexes = tmp_path / ".indexes"
    indexes.mkdir()
    config = EffectiveConfig(
        default_user="t",
        vault_path=tmp_path,
        thoughts_dir=thoughts,
        index_dir=indexes,
        embedding_model=DEFAULT_EMBEDDING_MODEL,
        vault_name="default",
        sync=SyncConfig(),
        llm=LLMConfig(),
    )
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    for code in check_codes.ALL_PHASE_2_CHECK_CODES:
        assert statuses.get(code) is CheckStatus.OK


# === conflict markers ===


def test_conflict_markers_present_fails(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    (repo / "thoughts" / "conflict.md").write_text(
        "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
    )
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.CONFLICT_MARKERS_PRESENT] is CheckStatus.FAIL


def test_conflict_markers_absent_is_ok(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.CONFLICT_MARKERS_PRESENT] is CheckStatus.OK


# === read-only role contradiction ===


def test_read_only_with_auto_push_fails_doctor(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    config = config.model_copy(
        update={"sync": SyncConfig(role="read-only", auto_push_on_capture=True)}
    )
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH] is CheckStatus.FAIL


def test_read_only_consistent_is_ok(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    config = config.model_copy(
        update={"sync": SyncConfig(role="read-only", auto_push_on_capture=False)}
    )
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.READ_ONLY_ROLE_CONTRADICTS_AUTO_PUSH] is CheckStatus.OK


# === gitignore ===


def test_gitignore_missing_fails_doctor(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path, gitignore_ok=False)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.GITIGNORE_INDEXES] is CheckStatus.FAIL


def test_gitignore_present_is_ok(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.GITIGNORE_INDEXES] is CheckStatus.OK


# === branch alignment ===


def test_branch_alignment_default_main_is_ok(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    # Branch is 'main', sync.git_branch defaults to 'main' -> OK.
    assert statuses[check_codes.BRANCH_ALIGNMENT] in {
        CheckStatus.OK,
        CheckStatus.WARN,  # if no remote, probe_remote_default_branch may warn
    }


# === working tree dirty ===


def test_dirty_tree_fails_doctor(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    (repo / "uncommitted.md").write_text("dirty")
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.WORKING_TREE_DIRTY_AT_STARTUP] is CheckStatus.FAIL


# === user identity ===


def test_user_identity_set_is_ok(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.SYNC_USER_IDENTITY_SET] is CheckStatus.OK


def test_user_identity_unset_warns_doctor(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    run_git(["config", "--unset", "user.email"], repo)
    run_git(["config", "--unset", "user.name"], repo)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.SYNC_USER_IDENTITY_SET] is CheckStatus.WARN


# === vault identity ===


def test_vault_identity_no_remote_is_ok(tmp_path: Path) -> None:
    # No remote configured -> contamination check is skipped -> status OK.
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    statuses = {c.name: c.status for c in report.checks}
    assert statuses[check_codes.VAULT_IDENTITY_REMOTE_MATCH] is CheckStatus.OK


# === all 14 codes appear in output ===


def test_run_sync_diagnostics_emits_every_code(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    names = {c.name for c in report.checks}
    for code in check_codes.ALL_PHASE_2_CHECK_CODES:
        assert code in names


# === full doctor end-to-end ===


def test_full_doctor_includes_sync_section(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=384,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    class _StubEmb:
        @property
        def model_name(self) -> str:
            return DEFAULT_EMBEDDING_MODEL

        @property
        def dimension(self) -> int:
            return 384

        def embed(self, text: str) -> list[float]:
            return [0.0] * 384

        async def aembed(self, text: str) -> list[float]:
            return [0.0] * 384

    report = run_diagnostics(config, embedder_factory=lambda c: _StubEmb())
    names = {c.name for c in report.checks}
    # Single-vault checks present.
    assert "thoughts_dir" in names
    # Sync checks present.
    assert check_codes.CONFLICT_MARKERS_PRESENT in names
    assert check_codes.GITIGNORE_INDEXES in names


def test_doctor_skip_sync_checks_flag(tmp_path: Path) -> None:
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    storage = VaultStorage(
        thoughts_dir=config.thoughts_dir,
        index_db_path=config.index_dir / "engram.db",
        embedding_dim=384,
        embedding_model_name=DEFAULT_EMBEDDING_MODEL,
    )
    storage.close()

    class _StubEmb:
        @property
        def model_name(self) -> str:
            return DEFAULT_EMBEDDING_MODEL

        @property
        def dimension(self) -> int:
            return 384

        def embed(self, text: str) -> list[float]:
            return [0.0] * 384

        async def aembed(self, text: str) -> list[float]:
            return [0.0] * 384

    report = run_diagnostics(
        config,
        embedder_factory=lambda c: _StubEmb(),
        skip_sync_checks=True,
    )
    names = {c.name for c in report.checks}
    assert check_codes.GITIGNORE_INDEXES not in names


@pytest.mark.parametrize("code", list(check_codes.ALL_PHASE_2_CHECK_CODES))
def test_each_code_surfaces_at_least_once(tmp_path: Path, code: str) -> None:
    """Every code must appear in run_sync_diagnostics output for some vault."""
    repo = _seeded_git_vault(tmp_path)
    config = _config_for(repo)
    report = DoctorReport()
    run_sync_diagnostics(report, config)
    names = {c.name for c in report.checks}
    assert code in names
