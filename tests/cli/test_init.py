"""Regression tests for ``engram init`` artifacts."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from engram.cli import app

runner = CliRunner()


def test_init_creates_skeleton(tmp_path: Path) -> None:
    """``engram init`` writes thoughts/, .indexes/, engram.config.yaml,
    .gitignore, README.md."""
    target = tmp_path / "vault"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0, result.stdout
    assert (target / "thoughts").is_dir()
    assert (target / ".indexes").is_dir()
    assert (target / "engram.config.yaml").is_file()
    assert (target / ".gitignore").is_file()
    assert (target / "README.md").is_file()


def test_init_gitignore_includes_phase_2_required_patterns(tmp_path: Path) -> None:
    """Regression: the ``engram doctor`` ``gitignore_indexes`` Phase 2 probe
    requires both ``.indexes/`` AND ``*.sqlite`` substrings. The init
    template MUST include them so a freshly initialized vault doctor-passes."""
    target = tmp_path / "vault"
    result = runner.invoke(app, ["init", str(target)])
    assert result.exit_code == 0
    body = (target / ".gitignore").read_text()
    assert ".indexes/" in body
    assert "*.sqlite" in body
    assert "*.sqlite-wal" in body
    assert "*.sqlite-shm" in body
    # Identity file is machine-local; must never get pushed.
    assert ".engram/identity.local" in body


def test_init_refuses_to_overwrite_existing_vault(tmp_path: Path) -> None:
    target = tmp_path / "vault"
    runner.invoke(app, ["init", str(target)])
    second = runner.invoke(app, ["init", str(target)])
    assert second.exit_code == 2


def test_init_with_explicit_vault_name(tmp_path: Path) -> None:
    target = tmp_path / "vault"
    result = runner.invoke(app, ["init", str(target), "--vault-name", "personal"])
    assert result.exit_code == 0
    body = (target / "engram.config.yaml").read_text()
    assert "vault_name: personal" in body
