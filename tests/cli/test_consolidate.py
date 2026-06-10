"""Tests for the ``engram consolidate`` CLI (Typer runner level)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from engram.cli import app
from engram.utils.lock import VaultLock

runner = CliRunner()


def _args(vault: Path, *extra: str) -> list[str]:
    return ["consolidate", *extra, "--config", str(vault / "engram.config.yaml")]


_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _setup_vault(tmp_path: Path) -> Path:
    """Vault with two exact-duplicate thoughts; targeted via explicit --config
    (the user-config path is import-time-resolved and not monkeypatchable)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "engram.config.yaml").write_text(
        yaml.safe_dump(
            {
                "vault_name": "primary",
                "thoughts_dir": str(vault / "thoughts"),
            }
        )
    )

    from engram.storage.facade import VaultStorage

    storage = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        vault_name="primary",
    )
    try:
        # Identical content -> exact-duplicate cluster needing no embeddings/LLM.
        storage.capture(content="[Lesson] duplicated wisdom", created_at=_NOW - timedelta(days=9))
        storage.capture(content="[Lesson] duplicated wisdom", created_at=_NOW - timedelta(days=1))
    finally:
        storage.close()
    return vault


def test_help_renders_and_command_registered():
    result = runner.invoke(app, ["consolidate", "--help"])
    assert result.exit_code == 0
    assert "--apply" in result.output
    assert "NOT deletion" in result.output


def test_report_mode_finds_exact_duplicates(tmp_path: Path):
    vault = _setup_vault(tmp_path)
    result = runner.invoke(app, _args(vault, "--no-llm"))
    assert result.exit_code == 0, result.output
    assert "1 actionable" in result.output
    reports = list((vault / ".indexes" / "consolidate").glob("report-*.json"))
    assert len(reports) == 1
    # Zero vault mutation: both thoughts still present.
    assert len(list((vault / "thoughts").rglob("*.md"))) == 2


def test_report_mode_without_index_refuses(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "thoughts").mkdir(parents=True)
    (vault / "engram.config.yaml").write_text(
        yaml.safe_dump({"vault_name": "primary", "thoughts_dir": str(vault / "thoughts")})
    )
    result = runner.invoke(app, _args(vault, "--no-llm"))
    assert result.exit_code == 2
    assert "does not exist" in result.output


def test_apply_without_report_refuses(tmp_path: Path):
    vault = _setup_vault(tmp_path)
    result = runner.invoke(app, _args(vault, "--apply", "--yes"))
    assert result.exit_code == 2
    assert "no report found" in result.output


def test_apply_executes_keep_newest(tmp_path: Path):
    vault = _setup_vault(tmp_path)
    report_run = runner.invoke(app, _args(vault, "--no-llm"))
    assert report_run.exit_code == 0, report_run.output
    result = runner.invoke(app, _args(vault, "--apply", "--yes"))
    assert result.exit_code == 0, result.output
    assert "Applied 1 cluster(s)" in result.output
    assert "not a git repository" in result.output
    assert len(list((vault / "thoughts").rglob("*.md"))) == 1
    assert len(list((vault / "archive").rglob("*.md"))) == 1


def test_apply_typed_confirmation_abort(tmp_path: Path):
    vault = _setup_vault(tmp_path)
    runner.invoke(app, _args(vault, "--no-llm"))
    result = runner.invoke(app, _args(vault, "--apply"), input="nope\n")
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert len(list((vault / "thoughts").rglob("*.md"))) == 2


def test_apply_refused_while_daemon_holds_vault(tmp_path: Path):
    vault = _setup_vault(tmp_path)
    runner.invoke(app, _args(vault, "--no-llm"))
    holder = VaultLock(vault)
    holder.acquire()
    try:
        result = runner.invoke(app, _args(vault, "--apply", "--yes"))
        assert result.exit_code == 2
        assert "daemon stop" in result.output
    finally:
        holder.release()
    assert len(list((vault / "thoughts").rglob("*.md"))) == 2


def test_apply_refused_on_team_vault_shape(tmp_path: Path):
    vault = _setup_vault(tmp_path)
    runner.invoke(app, _args(vault, "--no-llm"))
    members_dir = vault / ".engram"
    members_dir.mkdir(exist_ok=True)
    (members_dir / "members.yaml").write_text("members: []\n")
    result = runner.invoke(app, _args(vault, "--apply", "--yes"))
    assert result.exit_code == 2
    assert "team-write" in result.output


def test_stale_report_partial_apply_exits_three(tmp_path: Path):
    """A thought edited between report and apply skips its proposal; the run
    signals partial application via exit code 3."""
    vault = _setup_vault(tmp_path)
    assert runner.invoke(app, _args(vault, "--no-llm")).exit_code == 0

    from engram.storage.facade import VaultStorage

    storage = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        vault_name="primary",
    )
    try:
        thoughts, _total = storage.list_thoughts(limit=10)
        storage.update_body(thoughts[0].id, new_content="[Lesson] edited after the report")
    finally:
        storage.close()

    result = runner.invoke(app, _args(vault, "--apply", "--yes"))
    assert result.exit_code == 3
    assert "did not apply" in result.output
    # Nothing was archived: the only proposal was skipped.
    assert not (vault / "archive").exists()


def test_llm_resolution_scopes_to_prefix(tmp_path: Path):
    """A sensitive thought OUTSIDE the --prefix scope must not disable the
    LLM for an all-portable prefix run (it can never reach a prompt)."""
    from engram.cli.consolidate import _build_llm_callables
    from engram.config.models import DEFAULT_EMBEDDING_MODEL, EffectiveConfig, LLMConfig, SyncConfig
    from engram.storage.facade import VaultStorage

    vault = tmp_path / "vault"
    storage = VaultStorage(
        thoughts_dir=vault / "thoughts",
        index_db_path=vault / ".indexes" / "engram.db",
        vault_name="primary",
    )
    try:
        storage.capture(content="[Lesson] portable knowledge")
        storage.capture(content="[Decision] private call", portability="sensitive")
        config = EffectiveConfig(
            default_user="test",
            vault_path=vault,
            thoughts_dir=vault / "thoughts",
            index_dir=vault / ".indexes",
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            vault_name="primary",
            sync=SyncConfig(),
            llm=LLMConfig(provider="anthropic"),  # remote: refuses sensitive
        )
        judge, distiller, notice = _build_llm_callables(
            config, storage.conn, no_llm=False, prefix="Lesson"
        )
        assert judge is not None
        assert distiller is not None
        assert notice is None

        judge_all, _, notice_all = _build_llm_callables(
            config, storage.conn, no_llm=False, prefix=None
        )
        assert judge_all is None
        assert "LLM unavailable" in (notice_all or "")
    finally:
        storage.close()
