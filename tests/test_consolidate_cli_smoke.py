"""Hermetic CLI smoke for ``engram consolidate`` against the installed binary.

Each test spawns the actual ``engram`` binary via subprocess and asserts
observable state (filesystem layout, exit codes, stderr classification).
Per the engram CLAUDE.md "test the binary, not just the suite" discipline:
the unit/integration tests assert handler correctness; these assert the
wiring between the handlers and the user-facing binary.

The vault fixture seeds two exact-duplicate thoughts with PENDING
embeddings: the fingerprint pre-pass needs no embeddings and keep-newest
apply needs no LLM and no FastEmbed model download, so the smoke stays
hermetic (no network).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from engram.utils.lock import VaultLock

_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _engram_bin() -> str:
    binary = shutil.which("engram")
    if binary is None:
        pytest.skip("engram binary not on PATH; run `uv sync` then `uv pip install -e .`")
    return binary


def _smoke_env() -> dict[str, str]:
    return {
        **os.environ,
        "COLUMNS": "200",
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def _run(
    args: list[str],
    *,
    input_str: str | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - args are static literals + tmp paths
        [_engram_bin(), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=_smoke_env(),
        input=input_str,
    )


def _consolidate(vault: Path, *extra: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return _run(
        ["consolidate", *extra, "--config", str(vault / "engram.config.yaml")],
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def smoke_vault() -> Iterator[Path]:
    """Short-path vault seeded with two exact-duplicate pending-embedding thoughts."""
    with tempfile.TemporaryDirectory(prefix="eng-cons-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
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
            storage.capture(
                content="[Lesson] duplicated wisdom", created_at=_NOW - timedelta(days=9)
            )
            storage.capture(
                content="[Lesson] duplicated wisdom", created_at=_NOW - timedelta(days=1)
            )
        finally:
            storage.close()
        yield vault


def test_report_mode_exit_zero_and_writes_report(smoke_vault: Path):
    result = _consolidate(smoke_vault, "--no-llm")
    assert result.returncode == 0, result.stderr
    assert "1 actionable" in result.stdout
    assert "pending" in result.stdout  # exclusion accounting surfaces loudly
    reports = list((smoke_vault / ".indexes" / "consolidate").glob("report-*.json"))
    assert len(reports) == 1
    # Report mode mutated nothing.
    assert len(list((smoke_vault / "thoughts").rglob("*.md"))) == 2


def test_apply_keep_newest_end_to_end(smoke_vault: Path):
    assert _consolidate(smoke_vault, "--no-llm").returncode == 0
    result = _consolidate(smoke_vault, "--apply", "--yes")
    assert result.returncode == 0, result.stderr
    assert "Applied 1 cluster(s)" in result.stdout
    thoughts = list((smoke_vault / "thoughts").rglob("*.md"))
    archived = list((smoke_vault / "archive").rglob("*.md"))
    assert (len(thoughts), len(archived)) == (1, 1)
    archived_text = archived[0].read_text(encoding="utf-8")
    assert "archived_at" in archived_text
    assert "superseded_by" in archived_text
    # The newest duplicate survived in the index-facing tree.
    assert "duplicated wisdom" in thoughts[0].read_text(encoding="utf-8")


def test_apply_refused_while_lock_held(smoke_vault: Path):
    assert _consolidate(smoke_vault, "--no-llm").returncode == 0
    holder = VaultLock(smoke_vault)
    holder.acquire()
    try:
        result = _consolidate(smoke_vault, "--apply", "--yes")
        assert result.returncode == 2
        assert "daemon stop" in result.stderr
    finally:
        holder.release()
    assert len(list((smoke_vault / "thoughts").rglob("*.md"))) == 2


def test_apply_refused_on_team_vault(smoke_vault: Path):
    assert _consolidate(smoke_vault, "--no-llm").returncode == 0
    (smoke_vault / ".engram").mkdir(exist_ok=True)
    (smoke_vault / ".engram" / "members.yaml").write_text("members: []\n")
    result = _consolidate(smoke_vault, "--apply", "--yes")
    assert result.returncode == 2
    assert "team-write" in result.stderr


def test_report_without_index_refuses_with_remediation():
    with tempfile.TemporaryDirectory(prefix="eng-cons-", dir="/tmp") as root:
        vault = Path(root) / "vault"
        (vault / "thoughts").mkdir(parents=True)
        (vault / "engram.config.yaml").write_text(
            yaml.safe_dump({"vault_name": "primary", "thoughts_dir": str(vault / "thoughts")})
        )
        result = _consolidate(vault, "--no-llm")
        assert result.returncode == 2
        assert "does not exist" in result.stderr


def test_typed_confirmation_gate(smoke_vault: Path):
    assert _consolidate(smoke_vault, "--no-llm").returncode == 0
    result = _consolidate(smoke_vault, "--apply", input_str="wrong-token\n")
    assert result.returncode == 1
    assert len(list((smoke_vault / "thoughts").rglob("*.md"))) == 2
